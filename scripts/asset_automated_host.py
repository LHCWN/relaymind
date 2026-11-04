"""Prepare automated host experiment bindings; never dispatch a paid run.

RM16/RM40/RM44; AC31/AC36; spec 0.67 section 12.6.7. Commit the resulting
preregistration alongside its inputs before execution. This command does not
assert that those inputs are committed, authentic, qualified or executed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.asset_automated_host import bind_inputs  # noqa: E402
from relaymind.evaluation.asset_benchmark_io import json_bytes, read_json_path  # noqa: E402
from relaymind.evaluation.asset_corpus_io import DOCUMENT_BYTES, document  # noqa: E402
from relaymind.evaluation.asset_host_runner import host_preregistration  # noqa: E402
from relaymind.recommendations.private_files import create_private_file_exclusive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("protocol", "source-tasks", "source-lock", "tasks", "freeze")
    for name in names:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        inputs = {}
        for name in names:
            key = name.replace("-", "_")
            path = getattr(args, key).absolute()
            inputs[key] = (
                document(path.parent, path.name, "lock")
                if key == "source_lock"
                else read_json_path(path, DOCUMENT_BYTES)[0]
            )
        protocol = bind_inputs(**inputs)
        record = dict(
            schema_version=1,
            profile="automated_host",
            execution_status="not_run",
            protocol_digest=canonical_digest(protocol),
            source_tasks_digest=protocol["source_tasks_digest"],
            source_lock_digest=protocol["source_lock_digest"],
            host_preregistration=host_preregistration(inputs["tasks"], inputs["freeze"]),
            qualification="unverified",
            limitations=[
                "catalog_originals_not_verified",
                "not_committed_by_this_command",
                "no_execution_or_review_evidence",
            ],
        )
        if not create_private_file_exclusive(args.output.absolute(), json_bytes(record)):
            raise ValueError("output_exists")
    except (ValueError, OSError) as error:
        # Inputs and private paths never appear in the command's error text.
        print("preregistration refused: " + type(error).__name__, file=sys.stderr)
        return 2
    print("Preregistration written; no run started. Commit it with its frozen inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
