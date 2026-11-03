"""RM16/RM39 AC30: diagnostic real-host retained borrow capture; no qualification."""

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "ordinary_probe", HERE.parent / "host-ordinary-qualification/qualify.py"
)
ordinary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ordinary)
Base = ordinary.OrdinaryComposition


class RetainedComposition(Base):
    def configure(self, config, work, origin):
        super().configure(config, work, origin)
        self.retained_file = work / "retained-events.jsonl"
        self.retained_file.write_text("")
        self.retained_file.chmod(0o600)
        config["plugins"]["allow"].append("relaymind-retained-probe")
        config["plugins"]["load"]["paths"].append(str(HERE))
        config["plugins"]["entries"]["relaymind-retained-probe"] = dict(
            enabled=True,
            config=dict(
                origin=origin,
                eventFile=str(self.retained_file),
                providerConfig={k: v for k, v in self.config.items() if k != "recommendations"},
            ),
        )
        config.setdefault("models", {}).setdefault("providers", {})["relaymind-retained-probe"] = {
            "baseUrl": self.config["baseUrl"],
            "api": "openai-completions",
            "apiKey": self.application_token,
            "models": [{"id": "relaymind-client-tools", "name": "Diagnostic relay"}],
        }
        config["agents"]["defaults"]["model"]["primary"] = (
            "relaymind-retained-probe/relaymind-client-tools"
        )

    def loaded_use_turn(self, rpc, page, session, case, **kwargs):
        response = rpc(page, "rmqualification.retained", session)
        case["retained_admission"] = response["frame"]
        try:
            return super().loaded_use_turn(rpc, page, session, case, **kwargs)
        finally:
            case["retained_probe_events"] = [
                json.loads(line) for line in self.retained_file.read_text().splitlines()
            ]

    def source_paths(self):
        return super().source_paths() + [
            HERE / name
            for name in (
                "qualify.py",
                "plugin.mjs",
                "probe.mjs",
                "openclaw.plugin.json",
                "package.json",
            )
        ]


ordinary.OrdinaryComposition = RetainedComposition
person = ordinary.BASE.person_module()
ordinary.BASE.person_module = lambda: person
original_script = person.socket_observer_script
person.socket_observer_script = lambda *args, **kwargs: original_script(*args, **kwargs).replace(
    '"relaymind.recommendations"', '"rmqualification.retained", "relaymind.recommendations"'
)
if __name__ == "__main__":
    raise SystemExit(ordinary.main())
