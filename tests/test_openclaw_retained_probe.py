"""RM16/RM39 AC30: diagnostic harness setup must not claim qualification."""

import importlib.util
from pathlib import Path


def test_retained_diagnostic_reuses_patched_person_module():
    """AC30: the browser actually admits the diagnostic RPC on its real socket."""
    path = (
        Path(__file__).parents[1]
        / "integrations/openclaw/test/host-loaded-use-qualification/qualify.py"
    )
    spec = importlib.util.spec_from_file_location("retained_diagnostic", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ordinary.BASE.person_module() is module.person
    plugin = path.parent.parent / "host-person-qualification"
    extension = type("Observation", (), {"observation": object()})()
    script = module.person.socket_observer_script(plugin, extension)
    # Both the fixed browser allowlist and its supplied additional-method list.
    assert script.count('"rmqualification.retained"') == 2
    assert "installNativeSocketObserver(window," in script


def test_retained_provider_does_not_copy_unbound_action_configuration(tmp_path, monkeypatch):
    """RM39: diagnostic provider is independent of later person binding setup."""
    path = (
        Path(__file__).parents[1]
        / "integrations/openclaw/test/host-loaded-use-qualification/qualify.py"
    )
    spec = importlib.util.spec_from_file_location("retained_diagnostic_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.Base, "configure", lambda *args: None)
    extension = object.__new__(module.RetainedComposition)
    extension.application_token = "fixture-token"
    extension.config = {"baseUrl": "http://127.0.0.1:9123", "recommendations": {}}
    config = {
        "plugins": {"allow": [], "load": {"paths": []}, "entries": {}},
        "agents": {"defaults": {"model": {}}},
    }
    extension.configure(config, tmp_path, "http://10.0.0.1:9124")
    probe = config["plugins"]["entries"]["relaymind-retained-probe"]["config"]
    assert probe["providerConfig"] == {"baseUrl": "http://127.0.0.1:9123"}
    assert extension.config["recommendations"] == {}


def test_retained_vps_diagnostic_binds_capture_without_qualifying_use():
    """RM16/RM39 AC30: actual failed library boundary is not a qualified fence."""
    import hashlib
    import json
    import re

    root = Path(__file__).parents[1]
    evidence = root / "docs/evidence/m10/retained-boundary-20261102"
    summary = json.loads((evidence / "summary.json").read_text())
    raw = (evidence / "capture.json").read_bytes()
    capture = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == summary["capture"]["sha256"]
    assert summary["qualification"] == "unavailable"
    assert summary["production_enabled"] is False
    assert summary["model"] == "deterministic_double"
    cases = capture["ordinary_skill_loaded_use"]["cases"]
    assert [case["case"] for case in cases] == ["positive", "stale", "deleted", "reset"]
    for case in cases[:3]:
        assert case["observed"] == "refused"
        assert case["requests"] == []
        rows = case["retained_probe_events"]
        assert any(row["phase"] == "payload_current" for row in rows)
        assert any(
            row["phase"] == "library_dispatch_throw"
            and row["reason"] == "authenticated_request_scope_unavailable"
            for row in rows
        )
    assert cases[3]["observed"] == "control_refused"
    for row in summary["source_inventory"]:
        path = root / row["path"]
        if path.exists():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    for row in capture["environment"]["fixture_plugin_sources"]:
        if "/host-loaded-use-qualification/" in row["path"]:
            assert hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() == row["sha256"]
    # Capture contains only documented home placeholders and disposable accounts.
    text = raw.decode()
    assert all(value == "vps-user" for value in re.findall(r"/home/([^/\s\"]+)", text))
    assert "/Users/" not in text
