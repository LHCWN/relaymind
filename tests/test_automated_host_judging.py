"""RM16/RM44; AC36: judging uses frozen model and prompt identities."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "evaluation/assets/automated-host-277/judge.py"
    spec = importlib.util.spec_from_file_location("automated_judging", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_judging_selects_frozen_model_without_changing_old_defaults():
    m = module()
    protocol = m.read(m.HERE / "protocol.json")
    entry = m.judging_identity(
        protocol, ["judge", "--template", "judge-a.txt", "--name", "judge-a"]
    )
    assert entry["model"] == "glm-5.3-flash"
    assert entry["temperature"] == 0


def test_judging_refuses_unfrozen_template_and_identity():
    m = module()
    protocol = m.read(m.HERE / "protocol.json")
    for args in (
        ["judge", "--template", "../owner-run-260/prompts/judge-a.txt", "--name", "judge-a"],
        ["judge", "--template", "judge-a.txt", "--name", "other"],
    ):
        with pytest.raises(ValueError):
            m.judging_identity(protocol, args)


def test_judging_refuses_changed_template_digest():
    m = module()
    protocol = m.read(m.HERE / "protocol.json")
    protocol["judges"][2]["prompt_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        m.judging_identity(protocol, ["adjudicate"])


@pytest.mark.parametrize("response_bytes", [8, 1024 * 1024 + 123])
def test_judging_retains_failed_response_and_unknown_charge(tmp_path, monkeypatch, response_bytes):
    m = module()
    calls = []

    class Ledger:
        path = tmp_path / "ledger.json"

        def reserve(self, *args):
            calls.append(("reserve", args))
            return 1

        def unknown(self, entry):
            calls.append(("unknown", entry))

        def settle(self, entry, usage):
            calls.append(("settle", usage))

    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "https://example.invalid/v4")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "private-test-key")

    def transport(url, payload, headers, timeout, **kwargs):
        assert timeout == 120
        assert b"private-test-key" not in payload
        return {"outcome": "completed", "status": 200, "body": b"x" * response_bytes}

    chat = m.bounded_judge_chat("glm-5.3-flash", 0, lambda p, n: len(p) + n, transport)
    with pytest.raises(ValueError):
        chat(Ledger(), "judge-a", [], tmp_path, "batch")
    assert [c[0] for c in calls] == ["reserve", "unknown"]
    captures = list(tmp_path.rglob("response.bin"))
    assert len(captures) == 1
    assert captures[0].read_bytes() == b"x" * response_bytes


def test_blind_and_apply_keep_original_rubric_directory():
    m = module()
    assert m.command_home("blind") == m.HERE.parent / "owner-run-260"
    assert m.command_home("apply") == m.HERE.parent / "owner-run-260"
    assert m.command_home("judge") == m.HERE


def test_blinding_manifest_binds_items_without_revealing_map(tmp_path):
    import json

    m = module()
    items = tmp_path / "items.json"
    digest = tmp_path / "map-digest.json"
    items.write_text(
        json.dumps(
            {"format": "relaymind.owner_run_blind_items.v1", "items": [{"blind_id": "blind"}]}
        )
    )
    digest.write_text(
        json.dumps(
            {
                "format": "relaymind.owner_run_blind_map_digest.v1",
                "map_canonical_sha256": "a" * 64,
                "items": 1,
            }
        )
    )
    manifest = m.blinding_manifest(items, digest)
    m.validate_blinding(manifest, items)
    assert manifest["map_canonical_sha256"] == "a" * 64
    assert "map" not in manifest
    items.write_text('{"items":[]}')
    with pytest.raises(ValueError, match="blinding_items"):
        m.validate_blinding(manifest, items)


def test_judging_recovers_completed_capture_without_second_dispatch(tmp_path, monkeypatch):
    import json

    from test_owner_run_260_scripts import run_host

    m = module()
    ledger = run_host.harness.Ledger(tmp_path / "ledger.json", 10000)
    calls = []
    answer = {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 5}}
    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "https://example.invalid/v4")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "fixture-key")

    def transport(url, payload, headers, timeout, *, capture_root):
        calls.append(payload)
        folder = capture_root / "worker"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"status": 200, "outcome": "completed"}))
        (folder / "response.bin").write_text(json.dumps(answer))
        (folder / "request.bin").write_bytes(payload)
        raise RuntimeError("crash after captured response")

    chat = m.bounded_judge_chat("model", 0, lambda p, n: 500, transport)
    with pytest.raises(RuntimeError):
        chat(ledger, "judge-a", [], tmp_path / "log", "batch")
    recovered = chat(
        run_host.harness.Ledger(tmp_path / "ledger.json", 10000),
        "judge-a",
        [],
        tmp_path / "log",
        "batch",
    )
    assert recovered == answer
    assert len(calls) == 1
    settled = run_host.harness.Ledger(tmp_path / "ledger.json", 10000)
    assert settled.total == 9
    assert len(settled.data["reservations"]) == 1
    assert chat(settled, "judge-a", [], tmp_path / "log", "batch") == answer
    assert settled.total == 9


def test_judging_refuses_replay_after_incomplete_dispatch(tmp_path, monkeypatch):
    from test_owner_run_260_scripts import run_host

    m = module()
    ledger = run_host.harness.Ledger(tmp_path / "ledger.json", 10000)
    calls = []
    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "https://example.invalid/v4")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "fixture-key")

    def transport(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("connection lost")

    chat = m.bounded_judge_chat("model", 0, lambda p, n: 500, transport)
    with pytest.raises(RuntimeError):
        chat(ledger, "judge-a", [], tmp_path / "log", "batch")
    with pytest.raises(ValueError, match="judge_invocation_incomplete"):
        chat(ledger, "judge-a", [], tmp_path / "log", "batch")
    assert len(calls) == 1
    assert ledger.total == 500


def test_paid_judging_requires_committed_blinding_before_ledger(tmp_path):
    import json

    m = module()
    items = tmp_path / "items.json"
    digest = tmp_path / "map-digest.json"
    manifest = tmp_path / "manifest.json"
    items.write_text(json.dumps({"format": "relaymind.owner_run_blind_items.v1", "items": []}))
    digest.write_text(
        json.dumps(
            {
                "format": "relaymind.owner_run_blind_map_digest.v1",
                "map_canonical_sha256": "a" * 64,
                "items": 0,
            }
        )
    )
    manifest.write_text(json.dumps(m.blinding_manifest(items, digest)))
    checked = []
    with pytest.raises(ValueError, match="blinding_commit_required"):
        m.require_blinding(lambda *args: checked.append(args), None, manifest, items)
    assert checked == []
    m.require_blinding(lambda *args: checked.append(args), "b" * 40, manifest, items)
    assert checked == [("b" * 40, [manifest])]
    items.write_text("{}")
    with pytest.raises(ValueError, match="blinding_items"):
        m.require_blinding(lambda *args: None, "b" * 40, manifest, items)


def test_shared_judge_cap_has_exclusive_lock(tmp_path):
    m = module()
    ledger = tmp_path / "ledger.json"
    with m.ledger_lock(ledger):
        with pytest.raises(ValueError, match="judge_ledger_locked"):
            with m.ledger_lock(ledger):
                pytest.fail("concurrent ledger admitted")
    with m.ledger_lock(ledger):
        pass


def test_adjudication_forwards_b_without_abbreviating_wrapper_options(tmp_path, monkeypatch):
    """RM16/RM44: both judge files reach the command without altering --batch."""
    from relaymind.evaluation.asset_automated_host import AutomatedHostRejected

    m = module()
    monkeypatch.setattr(
        m.sys,
        "argv",
        [
            str(m.HERE / "judge.py"),
            "--protocol",
            str(m.HERE / "protocol.json"),
            "--preregistration-commit",
            "invalid",
            "--blind-manifest",
            str(tmp_path / "manifest.json"),
            "adjudicate",
            "--items",
            str(tmp_path / "items.json"),
            "--a",
            str(tmp_path / "a.json"),
            "--b",
            str(tmp_path / "b.json"),
            "--ledger",
            str(tmp_path / "ledger.json"),
            "--cap",
            "300000",
            "--batch",
            "6",
        ],
    )
    # Reaching the source guard proves both wrapper parsers accepted the exact
    # subcommand options. Invalid provenance prevents every paid invocation.
    with pytest.raises(AutomatedHostRejected, match="preregistration_commit"):
        m.main()


@pytest.mark.parametrize(
    "answer",
    [
        "Saved <run-dir>/sandboxes/h008-provider_only/result.md; native language retained.",
        r"Saved C:\sandbox\h006-native\result.md; native language retained.",
        "Saved sandboxes/h009-provider_only/result.md; native language retained.",
        "Saved sandboxes/h027-recommendations/result.md; native language retained.",
    ],
)
def test_blind_answers_hide_arm_bearing_run_keys_without_removing_content(answer):
    """RM16/RM44; AC36: sandbox identities must not reveal arms to blind judges."""
    import re

    m = module()
    redacted = m.anonymize_run_identity(answer)
    assert not re.search(r"h\d{3}-(native|provider_only|recommendations)", redacted)
    assert "[RUN]" in redacted
    assert "result.md; native language retained." in redacted


def test_blinding_installs_anonymization_before_building_judge_items():
    """RM16/RM44: the live blind-item builder receives anonymized answer bytes."""
    from types import SimpleNamespace

    m = module()
    legacy = SimpleNamespace(final_answer=lambda root, row: "sandboxes/h008-provider_only/x")
    m.configure_blinding(legacy)
    assert legacy.final_answer(None, {}) == "sandboxes/[RUN]/x"


@pytest.mark.parametrize("missing", ["items", "map", "map-digest"])
def test_blind_missing_output_argument_reports_input_error(tmp_path, monkeypatch, missing):
    """RM16/RM44: reject incomplete CLI inputs before checking frozen artifacts."""
    m = module()
    args = [
        "judge.py",
        "--protocol",
        str(m.HERE / "protocol.json"),
        "--preregistration-commit",
        "invalid",
        "--blind-manifest",
        str(tmp_path / "manifest.json"),
        "blind",
    ]
    for name in ("items", "map", "map-digest"):
        if name != missing:
            args.extend(["--" + name, str(tmp_path / (name + ".json"))])
    monkeypatch.setattr(m.sys, "argv", args)
    with pytest.raises(ValueError, match="blinding_inputs"):
        m.main()
    assert list(tmp_path.iterdir()) == []
