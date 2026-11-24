# asset-router with four embedding models (2026-11-24)

Development diagnostics, not acceptance. The written record is in
[`docs/EVIDENCE.md`](../../EVIDENCE.md#2026-11-24--asset-router-with-four-embedding-models-rm47-ac42).

| File | What it holds |
| --- | --- |
| `select_compare.json` | `scripts/guidance_compare.py --by-lane` over the similar-skill holdout: the default (`word`) as the baseline, asset-router with `bge-m3`, granite, nomic and SKILLRET at router defaults, and a GLM picker. Macro and micro precision and recall overall, per language and per lane, with the earlier metrics. |
| `select_embedders_vs_bge.json` | The same, with asset-router on `bge-m3` as the baseline. |
| `select_router_experiments.json` | Each model with two more router configs: `recallUnmatched` true with `recallLimit` 1000, and that with `minScore` 0.40. |
| `select_router_experiments_vs_bge.json` | The same configs with asset-router on `bge-m3` at its default as the baseline, so the sign tests on precision and recall compare each config with it. |
| `embedding_probe.json` | `scripts/guidance_embedding_probe.py` per model, by embedding alone: how often the right skill ranks first or in the top 3 among the 70 holdout skills, and its cosine, for English, mixed Chinese and pure Chinese prompts; nomic also with its prefixes and SKILLRET with its query instruction. |
| `turn_runs.jsonl` | One line per real turn of the new asset-router arms over the same 40 prompts as 2026-11-23: arm, case ID, lane, hinted and loaded names, right or not, token counts and time. |
| `turn_sync.json` | Each new arm's warm-up and how the router answered the measured turns. |

The default, `bge-m3` and GLM picker rows are the 2026-11-23 runs; their turn rows are in
[`2026-11-23-asset-router-vs-default/turn_runs.jsonl`](../2026-11-23-asset-router-vs-default/turn_runs.jsonl).
These files hold no prompt text, model answer or skill body. The arm reports themselves are not kept here; the
prompts and expected answers are in `evaluation/assets/guidance-similar/similar.v1.json`.
