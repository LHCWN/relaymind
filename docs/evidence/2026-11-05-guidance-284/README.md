# Per-prompt guidance development runs (#284)

Development diagnostics, not acceptance. The expected answers are written by the developer or by a model, and every
`compare` reports `acceptance_eligible: false`. The written record is in
[`docs/EVIDENCE.md`](../../EVIDENCE.md#2026-11-05--per-prompt-guidance-development-runs-284).

Second round (spec 0.69; RM45, RM46; AC38 to AC40):

| File | What it holds |
| --- | --- |
| `similar_holdout_compare.json` | Each selector arm over the similar-skill holdout: metrics overall and per lane. |
| `similar_holdout_old_vs_new.json` | The same prompts through the code before and after this round. |
| `selector_v3_compare.json`, `selector_v3_before_compare.json` | The 128 cases of `cases.v3.json`, after and before. Written before `guidance_compare.py` showed Asked as unknown for these reports: read its `undecided_asked` of 0.0 as unknown. Every other number is unchanged with the current tool. |
| `similar_turn_runs.jsonl` | Real turns on similar skills, one line per turn. |
| `regression_turn_runs.jsonl` | Real turns on the first round's 40 prompts. |
| `workflow_turn_runs.jsonl` | Real turns on big workflows that need several skills. |
| `tools_turn_runs.jsonl` | Real turns on MCP tools over a fake server. |
| `strict_gateway.json` | Each strict-limit call through a real gateway, with a scripted model. |

Turn files hold arm, pre-squash commit, case ID, lane, hinted and loaded names, and token counts: no prompt text or
model answer. `strict_gateway.json` holds only its own test skills and results.

Some default arms ran on an earlier commit than the arms they are compared with. A default arm runs no RelayMind
code (no plugin, no gateway), so only the harness, the data and the host could differ. They do not:

| File | Default arms | Other arms | Harness changes between the two | Data and host |
| --- | --- | --- | --- | --- |
| `similar_turn_runs.jsonl` | `2c31d4e3` | `e92bb076` | MCP-tools grading, workflow cases, two new output fields; none used by one-skill runs | Same one-skill families and prompts; same OpenClaw pin |
| `tools_turn_runs.jsonl` | `09b48208` | `e92bb076` | Workflow cases and two new output fields only; tools grading already the same | Same; same pin |
| `workflow_turn_runs.jsonl` | `e92bb076` | `e92bb076` | None | Same |
| `regression_turn_runs.jsonl` | `2c31d4e3`, `747e7e19` | `747e7e19`, `e92bb076` | See "Faults these runs found" in the record; the regraded rows are marked there | Same |

`evaluation/assets/matrix-development/cases.v3.json` keeps the `dataset_id` of `cases.v2.json` on purpose. It is the
same dataset: the same 128 cases and labels, with only the evidence IDs of the one skill whose text changed upstream
moved (`tests/test_matrix_benchmark_v3.py` checks both). It has its own `digest` and `catalog_digest`, and the loader
pairs cases with a catalog by `catalog_digest` (`asset_diagnostic.py`), so v3 cases cannot run against the v2 catalog.

First round, kept as measured:

| File | What it holds |
| --- | --- |
| `selector_summary.json` | Each selector arm over the 128 cases of `cases.v3.json`, commit `9f823dd9`. |
| `turn_summary.json` | Each full-turn arm: 40 prompts, one real model. |
| `turn_runs.jsonl` | One line per real turn. Its grader could count a refused call as a load (see the record). |

The arm reports themselves are about 65 MB each and are not kept here.
