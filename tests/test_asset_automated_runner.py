"""RM16/RM37/RM44; AC28/AC36: bounded automated host runner over doubles."""

import functools
import hashlib
import importlib.util
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from relaymind.evaluation.asset_automated_host import AutomatedHostRejected
from relaymind.evaluation.asset_host_runner import run_host_plan
from relaymind.evaluation.asset_whole_task import _check_attempts, _check_row
from test_owner_run_260_scripts import run_host as legacy
from whole_task_helpers import ENVIRONMENT, freeze_doc, row, tasks_doc

CONTROL_LIMITS = {
    "recommendation_poll_seconds": 10,
    "readiness_seconds": 60,
    "control_request_seconds": 10,
}
RECOMMENDATION_ROW = ({"id": "t", "prompt_case_id": "p"}, "recommendations", "t-recommendations")


def test_legacy_hooks_keep_person_arm_refused():
    run = object.__new__(legacy.Run)
    run.prompts = {"p": "original"}
    assert run.supports_arm("native")
    assert run.supports_arm("provider_only")
    assert not run.supports_arm("recommendations")
    assert run.prepare_prompt({"prompt_case_id": "p"}, "native", "key", []) == "original"


@functools.cache
def _automated():
    path = Path(__file__).resolve().parents[1] / "evaluation/assets/automated-host-277/run.py"
    spec = importlib.util.spec_from_file_location("automated_277_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gate_call(model, response_body, outcome="completed"):
    return dict(
        request_body=json.dumps({"model": model}, separators=(",", ":")),
        response_body=response_body,
        response_status=200,
        response_observed=True,
        outcome=outcome,
    )


def _budget_args():
    """Command-line values matching the example budget and the frozen protocol."""
    return SimpleNamespace(cap=13000000, row_budget=250000, max_output=8192, context_limit=128000)


def _prepare_recommendation(run):
    return run.prepare_prompt(*RECOMMENDATION_ROW, [])


def test_deferred_settlement_keeps_reservation_until_evidence(tmp_path):
    module = _automated()
    budget = module.DeferredBudget(
        tmp_path / "ledger.json", cap=1000, row_budget=500, max_output=100
    )
    assert budget.reserve("task-native") is None
    budget.settle("task-native", "native", prompt=10, completion=5, charged=15)
    assert budget.spent() == 500
    budget.flush_settlement()
    assert budget.spent() == 15


def test_automated_arm_supported_without_person_authority():
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    assert run.supports_arm("recommendations")
    assert not run.supports_arm("invented")


def test_recommendation_config_binds_actual_manifest_bytes(tmp_path):
    module = _automated()
    manifest = tmp_path / "import.json"
    manifest.write_text("{}")
    settings = {"enabled": True, "user_id": "u", "workspace_id": "w"}
    production = tmp_path / "production.toml"
    production.write_text(
        '[assets]\nsources=[{source_id="s",kind="metadata_import",path="import.json"}]\n[recommendations]\nenabled=true\nuser_id="u"\nworkspace_id="w"\n'
    )
    frozen = dict(
        schema_version=1,
        generation_seconds=120,
        recommendations=settings,
        sources=[
            dict(
                source_id="s",
                kind="metadata_import",
                manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
                manifest_bytes=2,
            )
        ],
    )
    actual, sources = module.frozen_recommendations(production, frozen, 120)
    assert actual == settings
    assert sources[0]["path"] == str(manifest)
    manifest.write_text('{"changed":true}')
    with pytest.raises(AutomatedHostRejected, match="source_manifest"):
        module.frozen_recommendations(production, frozen, 120)


def test_gate_attempts_preserve_unknown_and_failed_usage():
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    calls = [
        _gate_call("driver-model", '{"choices":[]}'),
        _gate_call(
            "host-model",
            'data: {"usage":{"prompt_tokens":7,"completion_tokens":4,"total_tokens":11}}\n',
            outcome="timeout",
        ),
    ]
    attempts = run.gate_attempts("t-recommendations", calls, "capture", {"model": "fallback"})
    assert attempts[0]["usage_provenance"] == "unknown"
    assert attempts[1]["output_tokens"] == 4
    assert attempts[1]["outcome"] == "timeout"
    assert [a["model"] for a in attempts] == ["driver-model", "host-model"]
    _check_attempts({"attempts": attempts}, set())


def test_failed_proposal_retains_gate_attempt_and_durable_capture(tmp_path, monkeypatch):
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    run.evidence_root = tmp_path / "evidence"
    run.work = tmp_path / "private"
    run.secrets = ["not-a-real-secret"]
    run.budget = module.DeferredBudget(
        tmp_path / "ledger.json", cap=1000, row_budget=500, max_output=100
    )
    run.gate = SimpleNamespace(last_closed_row=None)

    def fail(self, task, arm, config):
        assert self.budget.reserve("t-recommendations") is None
        self.gate.last_closed_row = dict(
            calls=[_gate_call("driver", '{"usage":{"prompt_tokens":4,"completion_tokens":5}}')],
            prompt=4,
            completion=5,
            charged=9,
            refused=[],
        )
        raise ValueError("invalid decision")

    monkeypatch.setattr(module.legacy.Run, "execute", fail)
    row, refs = run.execute(
        {"id": "t"}, "recommendations", {"model": "host", "recommendation_config_digest": "a" * 64}
    )
    _check_row(row, {"recommendation_config_digest": "a" * 64}, set())
    assert row["status"] == "failed"
    assert len(row["attempts"]) == 1
    assert row["attempts"][0]["output_tokens"] == 5
    assert run.budget.spent() == 9
    assert all((run.evidence_root / r["path"]).is_file() for r in refs)
    final = json.loads(
        (run.evidence_root / "host-runs/t-recommendations/automated-row.json").read_text()
    )
    assert final == row
    assert "t-recommendations:automated-row.json" in final["evidence_ids"]


def test_committed_inputs_refuse_worktree_edits(tmp_path):
    module = _automated()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "protocol.json"
    path.write_text("{}")
    subprocess.run(["git", "-C", str(tmp_path), "add", "protocol.json"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    module.require_committed_inputs(commit, [path], root=tmp_path)
    path.write_text('{"changed":true}')
    with pytest.raises(AutomatedHostRejected, match="preregistration_bytes"):
        module.require_committed_inputs(commit, [path], root=tmp_path)


@pytest.mark.parametrize("fenced", [False, True])
def test_real_loopback_proposal_uses_reserved_gate_and_read_only_channel(
    tmp_path, monkeypatch, fenced
):
    module = _automated()
    from automated_host_transport import DeadlineRequestGate

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.reply()

        def reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.command, self.path, dict(self.headers), body))
            if self.path.endswith("chat/completions"):
                content = '{"candidate_id":null,"content_hash":null}'
                if fenced:
                    content = "```json\n" + content + "\n```"
                value = {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 5},
                }
            else:
                value = {
                    "state": "computing" if self.command == "POST" else "ready",
                    "recommendation_id": "rec",
                    "displayed_candidates": [],
                }
            payload = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    run = object.__new__(module.AutomatedRun)
    run.prompts = {"p": "original task"}
    run.protocol = {"driver": {"model": "fixture-model"}, "per_generation_seconds": 15}
    run.frozen_budget = dict(CONTROL_LIMITS)
    run.driver_template = b"Return a bounded JSON proposal."
    run.args = SimpleNamespace(max_output=8192)
    run.recommendations = {"user_id": "u", "workspace_id": "w"}
    run.gateway_url, run.gateway_token = url, "gateway-test-key"
    run.evidence_root, run.work = tmp_path / "evidence", tmp_path / "work"
    run.secrets = ["gateway-test-key", "provider-test-key"]
    run._row_refs = []
    run.budget = module.DeferredBudget(
        tmp_path / "ledger.json", cap=30000, row_budget=15000, max_output=8192
    )
    run.gate = DeadlineRequestGate(
        url + "/v1", max_output=8192, timeout=15, capture_root=tmp_path / "upstream"
    )
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "provider-test-key")
    try:
        run.gate.start()
        assert run.budget.reserve("t-recommendations") is None
        run.gate.open_row("t-recommendations", 15000)
        refs = []
        actual_prompt = run.prepare_prompt(
            {"id": "t", "prompt_case_id": "p"}, "recommendations", "t-recommendations", refs
        )
        charged = run.gate.close_row()
        assert charged["charged"] == 9
        marker = json.loads(
            (run.evidence_root / "host-runs/t-recommendations/host-dispatch.json").read_bytes()
        )
        assert marker["started_at"] <= charged["calls"][0]["started_at"]
        run.dispatch_evidence({"id": "t"}, "recommendations", "t-recommendations", refs)
        assert sum(r["id"] == "host-dispatch:t-recommendations" for r in refs) == 1
        assert actual_prompt.startswith("original task")
        assert "untrusted" in actual_prompt
        assert [item[:2] for item in received] == [
            ("POST", "/relay/v1/recommendations"),
            ("GET", "/relay/v1/recommendations/rec"),
            ("POST", "/v1/chat/completions"),
        ]
        assert all("X-RelayMind-User-Action-Token" not in item[2] for item in received)
        assert run._recommendation_ids == ["rec"]
        assert json.loads(received[-1][3])["max_tokens"] == 8192
    finally:
        run.gate.stop()
        server.shutdown()
        server.server_close()


def test_every_effective_budget_is_frozen():
    module = _automated()
    p = module.legacy.read(module.HERE / "protocol.json")
    budget = module.legacy.read(module.HERE / "budget.v2.example.json")
    args = _budget_args()
    module.validate_run_budget(args, p, budget)
    for key in ("cap", "row_budget", "max_output", "context_limit"):
        changed = SimpleNamespace(**vars(args))
        setattr(changed, key, getattr(changed, key) + 1)
        with pytest.raises(AutomatedHostRejected):
            module.validate_run_budget(changed, p, budget)


def test_conserving_driver_keeps_all_three_arms_and_never_replays(tmp_path, monkeypatch):
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    freeze = freeze_doc(task_ids=("t001",))
    tasks = tasks_doc(freeze, lanes=("en_to_en",))
    run.frozen = {c["arm"]: {"config": c} for c in freeze["configurations"]}
    run.evidence_root, run.work = tmp_path / "evidence", tmp_path / "work"
    run.secrets = ["fixture-secret"]
    run.budget = module.DeferredBudget(
        tmp_path / "ledger.json", cap=3000, row_budget=500, max_output=100
    )
    run.gate = SimpleNamespace(last_closed_row=None)
    executed = []

    def execute(self, task, arm, config):
        key = task["id"] + "-" + arm
        executed.append(key)
        assert self.budget.reserve(key) is None
        self.gate.last_closed_row = dict(
            calls=[
                _gate_call("fixture-model", '{"usage":{"prompt_tokens":4,"completion_tokens":5}}')
            ],
            prompt=4,
            completion=5,
            charged=9,
            refused=[],
        )
        result = row(task["id"], arm, success=None, attempts=[])
        result["evidence_ids"] = []
        return result, []

    monkeypatch.setattr(module.legacy.Run, "execute", execute)

    def drive():
        return run_host_plan(
            tasks,
            freeze,
            run.execute,
            tmp_path / "journal.jsonl",
            environment=ENVIRONMENT,
            artifacts=[],
            qualification="fixture",
            input_manifest={"path": None, "digest": "a" * 64, "bytes": 1},
            interrupted=lambda: False,
            skip_reason=run.skip_reason,
        )

    first = drive()
    assert len(executed) == 3
    assert {r["arm"] for r in first["rows"]} == {"native", "provider_only", "recommendations"}
    assert all(len(r["attempts"]) == 1 for r in first["rows"])
    assert run.budget.spent() == 27
    assert drive() == first
    assert len(executed) == 3


def test_gate_attempts_keep_reported_cached_and_reasoning_components():
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    attempts = run.gate_attempts(
        "task-native",
        [
            _gate_call(
                "m",
                '{"usage":{"prompt_tokens":10,"completion_tokens":5,'
                '"prompt_tokens_details":{"cached_tokens":3},'
                '"completion_tokens_details":{"reasoning_tokens":2}}}',
            )
        ],
        "capture",
        {"model": "m"},
    )
    assert attempts[0]["cached_tokens"] == 3
    assert attempts[0]["reasoning_tokens"] == 2
    assert attempts[0]["token_relationship"] == "unknown"


def test_large_gate_capture_is_durable_before_row_settlement(tmp_path):
    """RM16/RM44: a valid >1 MiB SSE capture must not lose the row evidence."""
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    run.evidence_root = tmp_path
    run._row_refs = []
    run.sanitize = lambda text: text
    refs = []
    payload = b"x" * (1024 * 1024 + 123)
    run.evidence("task-native", "gated-calls.json", payload, refs)
    assert (tmp_path / "host-runs/task-native/gated-calls.json").read_bytes() == payload
    assert refs[0]["bytes"] == len(payload)
    assert run._row_refs == refs


def test_driver_failure_keeps_prior_whole_row_dispatch_marker(proposal_runner, tmp_path):
    """RM16/RM44: whole-row dispatch predates recommendations, including failures."""
    module, run = proposal_runner
    task = {"id": "t", "prompt_case_id": "p"}
    refs = []

    def fail(*args, **kwargs):
        marker = tmp_path / "host-runs/t-recommendations/host-dispatch.json"
        assert marker.exists()
        value = json.loads(marker.read_bytes())
        assert set(value) == {"schema_version", "task_id", "arm", "started_at"}
        assert value["task_id"] == "t" and value["arm"] == "recommendations"
        assert value["started_at"] <= module.legacy.now()
        raise ValueError("driver unavailable")

    run._http = fail
    with pytest.raises(ValueError, match="driver unavailable"):
        run.prepare_prompt(task, "recommendations", "t-recommendations", refs)
    assert refs[0]["id"] == "host-dispatch:t-recommendations"
    assert run.dispatch_evidence(task, "recommendations", "t-recommendations", refs)
    assert sum(ref["id"] == "host-dispatch:t-recommendations" for ref in refs) == 1


def test_host_command_timestamp_is_distinct_from_earlier_row_marker(
    proposal_runner, tmp_path, monkeypatch
):
    """RM16: host elapsed time starts after preparation, without rewriting row evidence."""
    module, run = proposal_runner
    times = iter(["2026-11-02T10:00:00Z", "2026-11-02T10:00:01Z", "2026-11-02T10:00:09Z"])
    monkeypatch.setattr(module.legacy, "now", lambda: next(times))
    refs = []
    task = {"id": "t", "prompt_case_id": "p"}
    assert run.prepare_prompt(task, "native", "t-native", refs) == "original"
    path = tmp_path / "host-runs/t-native/host-dispatch.json"
    original = path.read_bytes()
    assert run.dispatch_evidence(task, "native", "t-native", refs) == "2026-11-02T10:00:09Z"
    assert path.read_bytes() == original
    assert sum(ref["id"] == "host-dispatch:t-native" for ref in refs) == 1


NULL_PROPOSAL = '{"candidate_id":null,"content_hash":null}'


@pytest.mark.parametrize(
    "text",
    [
        NULL_PROPOSAL,
        "  " + NULL_PROPOSAL + "  ",
        "```json\n" + NULL_PROPOSAL + "\n```",
        "```\n" + NULL_PROPOSAL + "\n```",
    ],
)
def test_driver_accepts_one_unambiguous_json_fence(text):
    """RM16/RM44: transport formatting never changes proposal fields."""
    expected = {"candidate_id": None, "content_hash": None}
    assert _automated().parse_driver_proposal(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "choose this: " + NULL_PROPOSAL,
        "```json\n" + NULL_PROPOSAL + "\n``` trailing",
        "```json\n" + NULL_PROPOSAL + "\n```\n```json\n" + NULL_PROPOSAL + "\n```",
    ],
)
def test_driver_rejects_ambiguous_prose_and_multiple_fences(text):
    """RM12/RM16: never guess which object is the driver's proposal."""
    with pytest.raises(ValueError):
        _automated().parse_driver_proposal(text)


@pytest.fixture
def proposal_runner(tmp_path, monkeypatch):
    """RM16/RM44: a deterministic recommendation-driver boundary, with no inference."""
    module = _automated()
    run = object.__new__(module.AutomatedRun)
    run.evidence_root = tmp_path
    run._row_refs = []
    run.sanitize = lambda text: text
    run.protocol = {"driver": {"model": "fixture"}, "per_generation_seconds": 15}
    run.frozen_budget = dict(CONTROL_LIMITS)
    run.driver_template = b"Return a bounded proposal."
    run.args = SimpleNamespace(max_output=100)
    run.prompts = {"p": "original"}
    run.gateway_token, run.gateway_url = "fixture", "http://gateway.invalid"
    run.recommendations = {"user_id": "u", "workspace_id": "w"}
    run.gate = SimpleNamespace(url="http://gate.invalid", row={"calls": [{}]})
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "fixture")
    return module, run


def _driver_reply(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.parametrize(
    "reply,reason",
    [
        ({}, "driver_response_invalid"),
        ({"error": {"message": "provider failed"}}, "driver_response_invalid"),
        ({"choices": []}, "driver_response_invalid"),
        ({"choices": None}, "driver_response_invalid"),
        ({"choices": [{}]}, "driver_response_invalid"),
        ({"choices": [None]}, "driver_response_invalid"),
        ({"choices": [{"message": None}]}, "driver_response_invalid"),
        ({"choices": [{"message": {}}]}, "driver_response_invalid"),
        (_driver_reply(None), "driver_response_invalid"),
        (_driver_reply([]), "driver_response_invalid"),
        ([], "driver_response_invalid"),
        (None, "driver_response_invalid"),
        (_driver_reply("not JSON"), "driver_proposal_invalid"),
        (_driver_reply("{}"), "fields"),
        (
            _driver_reply('{"candidate_id":"invented","content_hash":"' + "a" * 64 + '"}'),
            "candidate_mismatch",
        ),
    ],
)
def test_invalid_driver_reply_is_recorded_without_dispatch(
    proposal_runner, tmp_path, reply, reason
):
    """RM05/RM12/RM16/RM44: malformed replies and invalid choices cannot reach a host prompt."""
    module, run = proposal_runner
    responses = iter(
        [
            {"state": "ready", "recommendation_id": "rec", "displayed_candidates": []},
            reply,
        ]
    )
    run._http = lambda *args, **kwargs: next(responses)
    with pytest.raises(module.AutomatedHostRejected, match=reason):
        _prepare_recommendation(run)
    root = tmp_path / "host-runs/t-recommendations"
    assert json.loads((root / "driver-response.json").read_bytes()) == reply
    assert json.loads((root / "proposal.json").read_bytes()) == {
        "status": "rejected",
        "reason": reason,
    }
    assert not (root / "host-prompt.json").exists()
    assert run._driver_call_count == 1


@pytest.mark.parametrize("expired", [False, True])
def test_recommendation_poll_wait_preserves_absolute_deadline(
    proposal_runner, monkeypatch, expired
):
    """RM03/RM16: each computing poll waits, with no new request after the deadline."""
    module, run = proposal_runner
    now, sleeps, requests = [0.0], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))

    def request(url, *args, **kwargs):
        requests.append((url, kwargs["timeout"]))
        if len(requests) == 1:
            now[0] = 9.95 if expired else 0.0
            return {"state": "computing", "recommendation_id": "rec"}
        if len(requests) == 2:
            return {"state": "ready", "recommendation_id": "rec", "displayed_candidates": []}
        return _driver_reply(NULL_PROPOSAL)

    run._http = request
    if expired:
        with pytest.raises(module.AutomatedHostRejected, match="recommendation_timeout"):
            _prepare_recommendation(run)
        assert len(requests) == 1
        assert sleeps == [pytest.approx(0.05)]
        observed = run.evidence_root / "host-runs/t-recommendations/recommendation.json"
        assert json.loads(observed.read_bytes()) == {
            "state": "computing",
            "recommendation_id": "rec",
        }
        assert run._recommendation_ids == ["rec"]
    else:
        assert _prepare_recommendation(run).startswith("original")
        assert sleeps == [0.1]
        assert requests[1][1] == pytest.approx(9.9)


@pytest.mark.parametrize(
    "value",
    [0, -1, True, "10", float("inf"), float("nan"), pytest.param(10**1000, id="unrepresentable")],
)
def test_control_budget_requires_positive_finite_numbers(value):
    """RM03: future dispatch explicitly freezes every control-request time limit."""
    module = _automated()
    budget = module.legacy.read(module.HERE / "budget.v2.example.json")
    protocol = module.legacy.read(module.HERE / "protocol.json")
    for name in CONTROL_LIMITS:
        changed = dict(budget, **{name: value})
        with pytest.raises(module.AutomatedHostRejected, match="budget_value"):
            module.validate_run_budget(_budget_args(), protocol, changed)


def test_legacy_budget_cannot_silently_dispatch_with_unrecorded_control_limits():
    """RM03/RM16: historical frozen inputs are retained, never silently upgraded."""
    module = _automated()
    budget = module.legacy.read(module.HERE / "configs/budget.json")
    protocol = module.legacy.read(module.HERE / "protocol.json")
    assert budget["format"] == "relaymind.automated_host_task_budget.v1"
    with pytest.raises(module.AutomatedHostRejected, match="budget_shape"):
        module.validate_run_budget(_budget_args(), protocol, budget)


@pytest.mark.parametrize("identity", [None, "", 3])
def test_malformed_computing_view_is_retained_without_poll(proposal_runner, identity):
    """RM05/RM16: retain the malformed view without constructing a guessed endpoint."""
    module, run = proposal_runner
    view = {"state": "computing", "recommendation_id": identity}
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return view

    run._http = request
    with pytest.raises(module.AutomatedHostRejected, match="recommendation_response_invalid"):
        _prepare_recommendation(run)
    assert len(calls) == 1
    observed = run.evidence_root / "host-runs/t-recommendations/recommendation.json"
    assert json.loads(observed.read_bytes()) == view


def test_configured_poll_budget_is_used_and_recorded(proposal_runner, monkeypatch):
    """RM03/RM16: recorded configuration, not an implicit constant, bounds polling."""
    module, run = proposal_runner
    run.frozen_budget["recommendation_poll_seconds"] = 0.05
    now, timeouts = [0.0], []

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))

    def request(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return {"state": "computing", "recommendation_id": "rec"}

    run._http = request
    with pytest.raises(module.AutomatedHostRejected, match="recommendation_timeout"):
        _prepare_recommendation(run)
    assert timeouts == [0.05]
    root = run.evidence_root / "host-runs/t-recommendations"
    assert (
        json.loads((root / "automated-dispatch.json").read_bytes())["budgets"] == run.frozen_budget
    )


def test_future_budget_example_is_valid():
    """RM03/RM16: the documented future config is valid without rewriting v1 inputs."""
    module = _automated()
    budget = module.legacy.read(module.HERE / "budget.v2.example.json")
    protocol = module.legacy.read(module.HERE / "protocol.json")
    module.validate_run_budget(_budget_args(), protocol, budget)
