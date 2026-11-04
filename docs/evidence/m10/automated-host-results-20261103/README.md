# Automated host experiment results (#277)

RM16, RM35–RM37, RM40–RM44; AC26–AC28, AC31–AC36; specification 0.68,
section 12.6.7. This records the automated replacement authorized for #277.
It is not person-driven selection or full M10 qualification.

## Observed results

Execution finished on 2026-11-03 at the unchanged 13,000,000-token hard cap.
The ledger charged **12,948,713** conservative tokens, including failed work,
unknown-usage reservations and both output-limit diagnostic calls. The journal
conserves all 300 planned task/arm outcomes: 95 started and finished, and 205
explicitly unrun. No started or failed row was replayed.

| Arm | Planned | Completed | Failed | Cancelled | Unrun | Judged successes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Native | 100 | 5 | 26 | 0 | 69 | 4 |
| Provider only | 100 | 3 | 27 | 2 | 68 | 0 |
| Recommendation assistance | 100 | 1 | 27 | 4 | 68 | 1 |

Unrun outcomes are not measurements. Completed answers can fail their rubric;
failed/cancelled executions count as failures. The summary's success and cost
gates remain blocked. There is no supported quality or cost improvement claim.
181 attempts have unknown usage. Reported cohort tokens total 1,900,063, which
is incomplete and must not replace the conservative ledger charge.

## Judging and review

The initial blind-map digest was committed in `9e2d436`. Final review found
that three answer texts exposed their producing arms through sandbox paths.
The initial judgments and captures remain retained as a flawed blind pass.
Agreement between those judges was not accepted as proof of absent bias.

The correction in `7d96b76` anonymizes harness run identities before generating
blind items. A fresh blind-map digest was committed in `8824fc3` before new
stateless judge requests using the same frozen models, prompts and rubrics.
The corrected requests contain no arm-bearing sandbox run keys. Both judges
agreed on all nine answers, with the same five successes and no verdict changes.
The new private map was opened only after these judgments finished.

Cumulative judging consumption is **52,710 of 300,000 tokens**: 26,693 for the
initial pass and 26,017 for the corrected pass. Neither pass required an
adjudicator model call. No execution row was replayed.

Two separate OpenCode 2 contexts reviewed the initial and corrected claims.
Both corrective reviews report no remaining material findings; the final record
bindings verify. The separate completion assessment records this bounded
experiment as complete under section 12.6.7. Full qualification remains blocked.
Machine contexts are not human approvals. Review consumption is reported
separately from execution and judging.

## Source history and limitations

Original preregistration `8f796b1` predates the first dispatch. The retained
[execution history](../automated-host-20261102/README.md) records the first-row
artifact recovery and native output-limit correction. The failed first row
keeps its full 250,000-token reservation. The initial native requests did not
have a verified 8,192-token output bound; ten unknown native calls received a
separate conservative adjustment of 1,119,783 tokens. Both diagnostic calls
used 82 tokens. Source `11478a3` corrected the native Z.ai parameter before
further inference. These historical violations remain disclosed.

Judges A/B used source `11478a3`. A later exact-argument forwarding fix in
`2af0cbb` allowed offline adjudication/application; its first CLI failure
occurred before dispatch or reservation. It changed no prompts, budgets,
rubrics, model settings or completed judgments. Original and corrected code,
actual invocation metadata and the independent review are retained.

The legacy `grade-host` command rejected the companion inventory with
`inventory_membership`; that result is retained, not reported as passing.
Its expected host command differs from the legacy bundler's selection-command
inventory. Automated outcome/binding validation is separate from legacy
qualification, as required by section 12.6.7.

The catalog remains development evidence. Loaded managed skill/MCP use remains
disabled; the harness supplies only selected public candidate metadata as
untrusted data or abstains. Production manual selection and direct-action
authority remain available and unchanged. Shared-VPS test activity overlapped
part of execution, so timing is not an isolated performance measurement.

## Evidence access

The public record contains hashes and references. Runtime responses, source
originals, blind inputs and raw review captures remain in the restricted VPS
assembly at `/home/vps-user/relaymind-automated-277/review-assembly-corrected/`. A clone
alone cannot verify those originals. The private resolver preserves their
exact bytes; the large pinned OpenClaw archive is retained separately with its
verified distribution-origin record. Nothing is described as embedded binary
content when only its provenance record is included.

`load_automated_record` verifies file bindings and conservation; it always
returns `completion_verified: false`. The completion assessment must separately
inspect actual reviews, their findings, judging coverage and these limitations.

The [completion assessment](completion-assessment.json) records the separate
assessment of review findings and coverage. The [record](record.json),
[report](bundle/report.json) and [publication manifest](publication.json) bind
the public metadata to the retained private originals. Initial and corrective
review consumption is summarized in [review usage](review-usage.json), with
provider-reported fields kept separate rather than presented as a billing total.
