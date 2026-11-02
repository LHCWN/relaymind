"""Bounded private transport for the automated experiment (RM03/RM16/RM40/RM44).

A disposable process owns each upstream socket. The parent enforces a wall-clock
limit even when a provider continuously sends bytes. No retry is performed.
Credentials cross the process boundary only on stdin and are never captured.
"""

import base64
import http.server
import importlib.util
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _write_private(path: Path, data: bytes) -> None:
    if os.name == "nt":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from relaymind.recommendations.private_files import create_private_file_exclusive

        if not create_private_file_exclusive(path.absolute(), data):
            raise FileExistsError("capture_exists")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _worker() -> None:
    command = json.loads(sys.stdin.buffer.read())
    watchdog = threading.Timer(command["timeout"], lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        _worker_request(command)
    finally:
        watchdog.cancel()


def _worker_request(command: dict) -> None:
    payload = command["body"]
    request = urllib.request.Request(
        command["url"],
        data=base64.b64decode(payload) if payload is not None else None,
        headers=command["headers"],
        method=command["method"],
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    result = dict(status=502, content_type="application/json", outcome="upstream_error")
    _write_private(Path(command["response_path"]), b"")
    fd = os.open(command["response_path"], os.O_WRONLY)
    with os.fdopen(fd, "wb", buffering=0) as capture:
        try:
            opener = urllib.request.build_opener(NoRedirect())
            try:
                response = opener.open(request, timeout=command["timeout"])
            except urllib.error.HTTPError as error:
                response = error
            with response:
                _write_private(
                    Path(command["response_path"]).with_name("response-observed.json"),
                    json.dumps({"status": response.code}).encode(),
                )
                kind = response.headers.get("Content-Type", "application/json")
                if len(kind) > 256 or "\r" in kind or "\n" in kind:
                    kind = "application/octet-stream"
                result.update(status=response.code, content_type=kind, outcome="completed")
                size = 0
                reader = getattr(response, "read1", response.read)
                while chunk := reader(min(65536, command["max_response_bytes"] - size + 1)):
                    allowed = command["max_response_bytes"] - size
                    capture.write(chunk[:allowed])
                    os.fsync(capture.fileno())
                    if len(chunk) > allowed:
                        result.update(status=502, outcome="response_too_large")
                        break
                    size += len(chunk)
        except (OSError, ValueError):
            result.update(status=502, outcome="upstream_error")
    sys.stdout.write(json.dumps(result))


# Worker startup deliberately avoids importing the historical host runner and
# its model/evaluation dependencies; only the parent needs that interface.
if __name__ == "__main__":
    _worker()
    raise SystemExit(0)


def bounded_json_request(
    url: str,
    body: bytes | None,
    headers: dict,
    timeout: float,
    *,
    method: str = "POST",
    capture_root: Path | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    cancel_event: threading.Event | None = None,
) -> dict:
    """Return bounded response bytes and preserve originals in a private directory.

    Failed/partial responses stay in response.bin; body is then a closed error
    response and raw_body retains the partial bytes for accounting/evidence.
    """
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or method not in ("POST", "GET")
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(max_response_bytes) is not int
        or not 0 < max_response_bytes <= MAX_RESPONSE_BYTES
        or (body is not None and (not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES))
    ):
        raise ValueError("transport_limits_invalid")
    root = Path(capture_root) if capture_root is not None else Path(tempfile.mkdtemp())
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("capture_root_invalid")
    capture = root / uuid.uuid4().hex
    capture.mkdir(mode=0o700)
    _write_private(capture / "request.bin", body or b"")
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _write_private(capture / "started.json", json.dumps(dict(started_at=started_at)).encode())
    command = dict(
        url=url,
        body=base64.b64encode(body).decode() if body is not None else None,
        headers=headers,
        timeout=timeout,
        method=method,
        response_path=str((capture / "response.bin").absolute()),
        max_response_bytes=max_response_bytes,
    )
    result = dict(status=502, content_type="application/json", outcome="upstream_error")
    started = time.monotonic()
    process = None
    try:
        if cancel_event is not None and cancel_event.is_set():
            result.update(status=499, outcome="cancelled")
        else:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            data = json.dumps(command).encode()
            while True:
                remaining = timeout - (time.monotonic() - started)
                if cancel_event is not None and cancel_event.is_set():
                    result.update(status=499, outcome="cancelled")
                    break
                if remaining <= 0:
                    result.update(status=504, outcome="timeout")
                    break
                try:
                    stdout, _ = process.communicate(
                        data,
                        timeout=min(remaining, 0.05) if cancel_event else remaining,
                    )
                    if process.returncode == 0:
                        result = json.loads(stdout)
                    elif process.returncode == 124:
                        result.update(status=504, outcome="timeout")
                    break
                except subprocess.TimeoutExpired:
                    data = None
    except (OSError, ValueError):
        result.update(status=502, outcome="upstream_error")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
    response_path = capture / "response.bin"
    if not response_path.exists():
        _write_private(response_path, b"")
    raw = response_path.read_bytes()
    observed_path = capture / "response-observed.json"
    upstream_status = None
    if observed_path.exists():
        try:
            upstream_status = json.loads(observed_path.read_bytes())["status"]
        except (ValueError, KeyError):
            # A killed child may leave an incomplete observation. Unknown stays
            # unknown; a synthetic proxy status never proves an upstream call.
            pass
    result.update(response_observed=upstream_status is not None, upstream_status=upstream_status)
    result.update(
        started_at=started_at, ended_at=datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )
    _write_private(capture / "result.json", json.dumps(result).encode())
    result.update(capture_path=str(capture.absolute()), raw_body=raw)
    result["body"] = (
        raw
        if result["outcome"] == "completed"
        else json.dumps({"error": result["outcome"]}).encode()
    )
    if result["outcome"] != "completed":
        result["content_type"] = "application/json"
    return result


def _legacy():
    path = Path(__file__).resolve().parents[1] / "evaluation/assets/owner-run-260/run_host.py"
    spec = importlib.util.spec_from_file_location("_automated_host_legacy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeadlineRequestGate(_legacy().RequestGate):
    """Preserve the original reservation/accounting contract with bounded I/O."""

    def __init__(
        self,
        upstream: str,
        *,
        max_output: int,
        timeout: float = 120,
        capture_root: Path | None = None,
    ):
        super().__init__(upstream, max_output=max_output, timeout=timeout)
        self.capture_root = capture_root
        self.last_closed_row = None
        self._condition = threading.Condition()
        self._connections = set()
        self._closing = False
        self._cancel = threading.Event()

    def open_row(self, key: str, limit: int) -> None:
        with self._condition:
            if self._connections or self.row is not None:
                raise ValueError("row_still_active")
            self._closing = False
            self._cancel = threading.Event()
            super().open_row(key, limit)

    def account(self, row, call, raw: bytes) -> None:
        # The historical gate substitutes zero for a missing completion count.
        # This experiment retains the whole reservation for unknown usage.
        text = raw.decode("utf-8", "replace")
        try:
            documents = [json.loads(text)]
        except ValueError:
            documents = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        documents.append(json.loads(line[5:]))
                    except ValueError:
                        pass
        usages = [
            doc["usage"]
            for doc in documents
            if isinstance(doc, dict) and isinstance(doc.get("usage"), dict)
        ]
        if not usages or any(
            type(usages[-1].get(key)) is not int or usages[-1][key] < 0
            for key in ("prompt_tokens", "completion_tokens")
        ):
            return
        super().account(row, call, raw)

    def close_row(self) -> dict:
        with self._condition:
            target = self.row
            if target is None:
                if self.last_closed_row is None:
                    raise ValueError("no_reservation")
                return self.last_closed_row
            self._closing = True
            self._cancel.set()
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            while self.row is target and self._connections:
                self._condition.wait()
            if self.row is not target:
                # Another closer already drained this exact row. Do not close
                # a new row that may have opened while this waiter was asleep.
                return dict(
                    target,
                    calls=[dict(c) for c in target["calls"]],
                    refused=list(target["refused"]),
                )
            self.last_closed_row = super().close_row()
            return self.last_closed_row

    def stop(self) -> None:
        if self.row is not None:
            self.close_row()
        super().stop()

    def start(self) -> str:
        gate = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                with gate._condition:
                    if gate._closing:
                        self.reply(400, "application/json", b'{"error":"no_reservation"}')
                        return
                    gate._connections.add(self.connection)
                try:
                    self.handle_post()
                finally:
                    with gate._condition:
                        gate._connections.discard(self.connection)
                        gate._condition.notify_all()

            def handle_post(self):
                self.connection.settimeout(gate.timeout)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= MAX_REQUEST_BYTES:
                        raise ValueError
                    deadline = time.monotonic() + gate.timeout
                    chunks, remaining = [], length
                    while remaining:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise ValueError
                        self.connection.settimeout(left)
                        chunk = self.rfile.read1(min(remaining, 65536))
                        if not chunk:
                            raise ValueError
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    body = b"".join(chunks)
                    if self.path != "/v1/chat/completions":
                        raise ValueError
                except (ValueError, OSError):
                    self.reply(400, "application/json", b'{"error":"request_invalid"}')
                    return
                admitted, reason = gate.admit(body)
                if admitted is None:
                    self.reply(400, "application/json", json.dumps({"error": reason}).encode())
                    return
                payload, row, call = admitted
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() in ("authorization", "content-type", "accept")
                }
                result = bounded_json_request(
                    gate.upstream + self.path.removeprefix("/v1"),
                    payload,
                    headers,
                    gate.timeout,
                    capture_root=gate.capture_root,
                    cancel_event=gate._cancel,
                )
                with gate.lock:
                    call.update(
                        request_body=payload,
                        response_body=result["raw_body"],
                        response_status=result["status"],
                        response_observed=result["response_observed"],
                        upstream_status=result["upstream_status"],
                        outcome=result["outcome"],
                        started_at=result["started_at"],
                        ended_at=result["ended_at"],
                        capture_path=result["capture_path"],
                    )
                if result["outcome"] == "completed":
                    gate.account(row, call, result["raw_body"])
                self.reply(result["status"], result["content_type"], result["body"])

            def reply(self, status, kind, body):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.url
