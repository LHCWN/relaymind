# asset-router against the default (2026-11-23)

Development diagnostics, not acceptance. The written record is in
[`docs/EVIDENCE.md`](../../EVIDENCE.md#2026-11-23--asset-router-against-the-default-rm47-ac42).

| File | What it holds |
| --- | --- |
| `select_compare.json` | `scripts/guidance_compare.py --by-lane` over the similar-skill holdout: the default (`word`) as the baseline, asset-router, and a GLM picker. Metrics overall and per lane. |
| `select_router_vs_picker.json` | The same, with asset-router as the baseline and the GLM picker as the arm. |
| `turn_runs.jsonl` | One line per real turn, four arms over the same 40 prompts: arm, case ID, lane, hinted and loaded names, right or not, token counts and time. |
| `turn_sync.json` | The asset-router arm's warm-up and how the router answered the measured turns. |

These files hold no prompt text, model answer or skill body. The arm reports themselves are not kept
here; the prompts and expected answers are in `evaluation/assets/guidance-similar/similar.v1.json`.
