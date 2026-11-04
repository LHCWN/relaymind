"""RM03/RM16/RM40/RM44; AC31/AC36: bounded, captured automated host calls."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from helpers import load_script_module

# Allow cold interpreter startup on every platform, including response-read tests
# that must reach upstream first. The startup-only test still uses 0.1 seconds.
REQUEST_TIMEOUT = 15
STREAM_DEADLINE = REQUEST_TIMEOUT
# Nested requests own two workers, each with its own startup cost.
PROXY_TIMEOUT = 2 * REQUEST_TIMEOUT


@pytest.mark.parametrize(
    "fields",
    [
        {"max_completion_tokens": 5},
        {"max_completion_tokens": 15},
        {"max_completion_tokens": 8, "max_tokens": 3},
    ],
)
def test_zai_output_limit_uses_documented_field_before_budgeting(transport, fields):
    """RM03/RM16: the upstream cap must match the admitted output allowance."""
    gate = transport.DeadlineRequestGate("https://api.z.ai/api/paas/v4", max_output=10)
    gate.open_row("native", 10000)
    admitted, reason = gate.admit(json.dumps(dict(messages=[], **fields)).encode())
    assert reason is None
    payload, _, call = admitted
    body = json.loads(payload)
    assert "max_completion_tokens" not in body
    assert body["max_tokens"] == min(10, *fields.values())
    assert call["bound"] == transport._legacy().harness.request_bound(payload, body["max_tokens"])


def test_other_provider_keeps_its_completion_limit_field(transport):
    """RM03: provider-specific compatibility must not alter another endpoint."""
    gate = transport.DeadlineRequestGate("https://example.com/v1", max_output=10)
    gate.open_row("native", 10000)
    admitted, _ = gate.admit(b'{"messages":[],"max_completion_tokens":5}')
    assert json.loads(admitted[0])["max_completion_tokens"] == 5
    assert "max_tokens" not in json.loads(admitted[0])


@pytest.mark.parametrize("invalid", [0, -1, True, "5"])
def test_zai_invalid_output_limit_is_not_normalized_away(transport, invalid):
    """RM03: normalization cannot admit an invalid client limit."""
    gate = transport.DeadlineRequestGate("https://api.z.ai/api/paas/v4", max_output=10)
    gate.open_row("native", 10000)
    admitted, reason = gate.admit(json.dumps({"max_completion_tokens": invalid}).encode())
    assert admitted is None and reason == "request_invalid"


@pytest.fixture(scope="module")
def transport():
    return load_script_module("automated_host_transport")


@pytest.fixture
def started_gate(transport, tmp_path):
    gates = []

    def start(url, *, timeout=REQUEST_TIMEOUT):
        gate = transport.DeadlineRequestGate(
            url, max_output=10, timeout=timeout, capture_root=tmp_path
        )
        gate.open_row("row", 10000)
        gates.append(gate)
        return gate, gate.start()

    yield start
    for gate in gates:
        gate.stop()


@pytest.fixture
def upstream():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                if "/slow" in self.path:
                    while True:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.03)
                elif self.path.endswith("large"):
                    self.wfile.write(b"x" * 10000)
                else:
                    self.wfile.write(b'{"usage":{"prompt_tokens":2,"completion_tokens":3}}')
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    server.server_close()


def test_original_capture_is_private_and_excludes_credentials(transport, upstream, tmp_path):
    url, calls = upstream
    result = transport.bounded_json_request(
        url,
        b'{"messages":[]}',
        {"Authorization": "Bearer never-save-me"},
        REQUEST_TIMEOUT,
        capture_root=tmp_path,
    )
    assert result["status"] == 200 and result["outcome"] == "completed"
    capture = Path(result["capture_path"])
    assert (capture / "request.bin").read_bytes() == b'{"messages":[]}'
    assert (capture / "response.bin").read_bytes() == result["body"]
    for path in capture.iterdir():
        assert b"never-save-me" not in path.read_bytes()
        from relaymind.recommendations.private_files import open_private_file

        with open_private_file(path) as lease:
            assert lease.data == path.read_bytes()
    assert len(calls) == 1


def test_deadline_kills_worker_despite_continuously_arriving_bytes(
    transport, upstream, tmp_path, monkeypatch
):
    url, calls = upstream
    processes = []
    original = transport.subprocess.Popen

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", observe)
    started = time.monotonic()
    result = transport.bounded_json_request(
        url + "/slow", b"{}", {}, STREAM_DEADLINE, capture_root=tmp_path
    )
    assert time.monotonic() - started < STREAM_DEADLINE + 2
    assert result["outcome"] == "timeout" and result["status"] == 504
    assert len(processes) == 1 and processes[0].poll() is not None
    assert len(calls) == 1
    assert (Path(result["capture_path"]) / "response.bin").read_bytes()
    assert result["response_observed"] is True
    assert result["upstream_status"] == 200


def test_deadline_includes_worker_startup(transport, upstream, tmp_path, monkeypatch):
    """RM03/RM16: a worker that has not sent a request is still bounded."""
    url, calls = upstream
    processes = []
    original = transport.subprocess.Popen

    def delayed_start(args, **kwargs):
        process = original([args[0], "-c", "import time; time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", delayed_start)
    started = time.monotonic()
    result = transport.bounded_json_request(url, b"{}", {}, 0.1, capture_root=tmp_path)
    assert time.monotonic() - started < 2
    assert result["status"] == 504 and result["outcome"] == "timeout"
    assert len(processes) == 1 and processes[0].poll() is not None
    assert calls == []
    assert result["response_observed"] is False
    assert result["upstream_status"] is None
    assert (Path(result["capture_path"]) / "response.bin").read_bytes() == b""


def test_response_cap_and_read_only_get(transport, upstream, tmp_path):
    url, _ = upstream
    result = transport.bounded_json_request(
        url + "/large",
        None,
        {},
        REQUEST_TIMEOUT,
        method="GET",
        capture_root=tmp_path,
        max_response_bytes=100,
    )
    assert result["outcome"] == "response_too_large" and result["status"] == 502
    assert len((Path(result["capture_path"]) / "response.bin").read_bytes()) <= 100


def test_gate_preserves_unknown_charge_and_never_replays(transport, upstream, started_gate):
    url, calls = upstream
    gate, endpoint = started_gate(url + "/slow", timeout=STREAM_DEADLINE)
    result = transport.bounded_json_request(
        endpoint + "/chat/completions", b"{}", {}, PROXY_TIMEOUT
    )
    assert result["status"] == 504
    row = gate.close_row()
    assert row["charged"] == row["calls"][0]["bound"]
    assert row["calls"][0]["usage"] is None
    assert row["calls"][0]["outcome"] == "timeout"
    assert gate.last_closed_row == row
    rejected = transport.bounded_json_request(
        endpoint + "/chat/completions", b"{}", {}, REQUEST_TIMEOUT
    )
    assert rejected["status"] == 400
    assert len(calls) == 1


def test_gate_accounts_completed_usage_and_retains_original_bytes(
    transport, upstream, started_gate
):
    url, _ = upstream
    gate, endpoint = started_gate(url)
    result = transport.bounded_json_request(
        endpoint + "/chat/completions", b"{}", {}, PROXY_TIMEOUT
    )
    assert result["status"] == 200
    row = gate.close_row()
    assert row["charged"] == 5
    call = row["calls"][0]
    assert json.loads(call["request_body"])["max_tokens"] == 10
    assert call["response_body"] == result["body"]
    assert call["started_at"] <= call["ended_at"]


@pytest.mark.parametrize(
    "usage", [{"prompt_tokens": 2}, {"prompt_tokens": -1, "completion_tokens": 2}]
)
def test_missing_or_invalid_usage_keeps_reservation(transport, usage):
    gate = transport.DeadlineRequestGate("http://127.0.0.1", max_output=10)
    gate.open_row("row", 10000)
    (payload, row, call), _ = gate.admit(b"{}")
    gate.account(row, call, json.dumps({"usage": usage}).encode())
    assert gate.charged() == call["bound"]
    assert call["usage"] is None


@pytest.mark.parametrize("action", ["close_row", "stop"])
def test_close_row_cancels_inflight_work_before_snapshot(transport, upstream, started_gate, action):
    url, calls = upstream
    gate, endpoint = started_gate(url + "/slow", timeout=PROXY_TIMEOUT)
    thread = threading.Thread(
        target=lambda: transport.bounded_json_request(
            endpoint + "/chat/completions",
            b"{}",
            {},
            PROXY_TIMEOUT + REQUEST_TIMEOUT,
        )
    )
    thread.start()
    deadline = time.monotonic() + PROXY_TIMEOUT
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls
    started = time.monotonic()
    getattr(gate, action)()
    row = gate.last_closed_row
    try:
        assert time.monotonic() - started < 1
        assert row["calls"][0]["outcome"] == "cancelled"
        assert row["charged"] == row["calls"][0]["bound"]
        assert Path(row["calls"][0]["capture_path"]).joinpath("result.json").exists()
    finally:
        gate.stop()
        thread.join(6)


def test_concurrent_closers_share_the_finished_snapshot(transport):
    from concurrent.futures import ThreadPoolExecutor

    gate = transport.DeadlineRequestGate("http://127.0.0.1", max_output=10)
    gate.open_row("row", 10000)
    calls = threading.Barrier(3)

    class Connection:
        def shutdown(self, _):
            pass

    connection = Connection()
    gate._connections.add(connection)

    def close():
        calls.wait()
        return gate.close_row()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(close), pool.submit(close)
        calls.wait()
        # Wait until both closers have entered the condition wait, so they both
        # see the same active row before its simulated request drains.
        deadline = time.monotonic() + 2
        while len(gate._condition._waiters) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        with gate._condition:
            gate._connections.clear()
            gate._condition.notify_all()
        assert first.result(timeout=2) == second.result(timeout=2) == gate.last_closed_row


def test_connection_failure_is_not_an_observed_response(transport, tmp_path):
    import socket

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        unavailable = f"http://127.0.0.1:{closed.getsockname()[1]}"
    failure = transport.bounded_json_request(unavailable, b"{}", {}, 10, capture_root=tmp_path)
    assert failure["status"] == 502
    assert failure["response_observed"] is False and failure["upstream_status"] is None


def test_worker_self_terminates_without_parent_timeout_enforcement(transport, upstream, tmp_path):
    import subprocess
    import sys

    url, _ = upstream
    command = dict(
        url=url + "/slow",
        body=None,
        headers={},
        method="GET",
        timeout=0.5,
        response_path=str(tmp_path / "response.bin"),
        max_response_bytes=100000,
    )
    process = subprocess.Popen(
        [sys.executable, transport.__file__],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        process.communicate(json.dumps(command).encode(), timeout=REQUEST_TIMEOUT)
        assert process.returncode == 124
        assert time.monotonic() - started < REQUEST_TIMEOUT
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("suffix", ["", "/", "///"])
def test_gate_normalizes_upstream_base_path(transport, upstream, started_gate, suffix):
    """RM16/RM44: a configured base path keeps exactly one endpoint separator."""
    url, calls = upstream
    _, endpoint = started_gate(url + "/provider/v4" + suffix)
    result = transport.bounded_json_request(
        endpoint + "/chat/completions", b"{}", {}, PROXY_TIMEOUT
    )
    assert result["status"] == 200
    assert calls == ["/provider/v4/chat/completions"]
