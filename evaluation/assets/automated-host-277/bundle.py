"""Bundle automated results without publishing runtime source excerpts.

RM12/RM16/RM44; AC36. Reuses the established input bundle and private resolver;
all runtime originals stay private because source redistribution rights vary.
"""

import argparse
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "automated_277_bundle_legacy", HERE.parent / "owner-run-260/bundle.py"
)
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)

from relaymind.recommendations.private_files import create_private_file_exclusive  # noqa: E402


def private_runtime_bundle(base, identities: set[str]):
    """Keep raw runtime evidence private while preserving exact hash bindings."""

    class PrivateBundle(base):
        def __init__(self, out, private):
            out, private = Path(out).resolve(), Path(private).resolve()
            if out.is_relative_to(private) or private.is_relative_to(out):
                raise ValueError("bundle_roots_overlap")
            super().__init__(out, private)

        def put_private(self, relative, payload):
            legacy.relative_path(relative)
            target = self.private / relative
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not create_private_file_exclusive(target, payload, maximum=16 * 1024 * 1024):
                raise ValueError("private_evidence_exists")

        def file(self, evidence_id, kind, access, payload, *, publish, relative=None):
            if evidence_id in identities:
                access, publish = "restricted", False
            return super().file(
                evidence_id, kind, access, payload, publish=publish, relative=relative
            )

        def write(self, report_path, report):
            for ref in report["evidence"]:
                if ref["id"] in identities:
                    ref["path"] = None
            return super().write(report_path, report)

    return PrivateBundle


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--report", type=Path, required=True)
    args, _ = parser.parse_known_args()
    report = legacy.read(args.report)
    legacy.Bundle = private_runtime_bundle(legacy.Bundle, {r["id"] for r in report["evidence"]})
    legacy.main()


if __name__ == "__main__":
    main()
