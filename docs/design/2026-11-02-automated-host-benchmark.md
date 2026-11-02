# Automated whole-task benchmark for #277

Status: specification 0.68 retains the automated experiment defined in 0.67.
Protocol/proposal validation, the bounded executor, durable preregistration,
blind judging and private evidence bundling are implemented. The live experiment
and its final review record remain pending.

Requirements: RM16, RM35–RM37, RM40–RM44; AC26–AC28, AC31–AC36.

## Intended result

Deliver the existing 100 paired host tasks without a person operating each
recommendation. Keep all three arms, complete denominators, original prompts,
labels and rubrics, durable raw results, blind success judging and whole-task
cost accounting. Describe the result as an automated benchmark. Completing
this benchmark does not establish person-driven operation, independent human
review, the acceptance catalog, or final M10 acceptance.

## Findings in the current implementation

`evaluation/assets/owner-run-260/run_host.py` refuses recommendations in both
`skip_reason` and `execute`. Its actual execution path is the host's `agent
exec` CLI. That caller is not the qualified Control UI person ingress.
Removing either refusal does not implement recommendations and cannot grant
choice authority.

The same directory already provides two blinded machine judges and a machine
adjudicator in `judge.py`. New judge infrastructure is unnecessary, but its
profile label and pinned templates must describe the new protocol accurately.

The frozen `lock.json` has purpose `development`. Original private catalog
bytes need rebuilding and their hashes checked. A complete run over this
catalog remains below qualified acceptance. Existing owner-run records also
require an actual independent organization-member review. A machine review
must never be entered in `reviewer_logins` as that approval.

## Approaches

1. Automate the native Control UI and describe the caller as a person. Reject:
   automation does not establish direct-person authority and conflicts with
   section 7.6.1.
2. Add a production model-callable choice capability. Reject for this issue:
   this changes product authorization and is substantially broader than an
   evaluation change.
3. Add an evaluation-only recommendation proposal arm. Recommended: the
   harness obtains public recommendation results, a pinned model proposes one
   displayed candidate or abstention, and the harness passes bounded public
   metadata to the host as explicitly untrusted task context. No gateway
   `choose`, feedback, reset or native managed-selection operation occurs.
   This measures automated recommendation assistance, not installed skill use.

The third approach is deliberately limited while #280 remains unqualified.
It neither injects selected instruction bytes nor claims that a skill loaded.
Native host tools still execute under the same frozen policy as the baseline.
It is a change to the experimental question and must be recorded as such.

## Specification change before implementation

Add a section 12.6.7 automated host benchmark profile in a new spec revision
and changelog entry. Keep owner_run and its human review contract unchanged.
Define a closed automated protocol document binding the original case/label
lock, new freeze/tasks digests, driver model/provider/sampling, prompt bytes,
maximum decision calls, validated decision shape, public candidate projection,
time/token budgets, judge templates, disabled operations and evidence policy.
A new preregistration commit binds this protocol before dispatch.

Retain the three-arm order and original task IDs/prompts/rubrics. Because
`HostTasks` embeds `freeze_digest` and budgets digests, produce a new task
wrapper and freeze in a new versioned run-input directory; do not overwrite
owner-run-260 files or pretend the old preregistration covers the new run.

The report remains provisional with respect to existing final acceptance.
Two separate machine review contexts replace the human experiment review for
this profile, with recorded findings and resolutions. Use a separate closed
automated execution record, not a forged approved
QualificationRecordV1. It binds protocol, report, inputs, code and captures;
records automated review findings/resolution; and explicitly declares no
person observation or independent organization approval. The issue's revised
completion criterion is delivery of this automated experiment and honest
grading, not a `qualified` gate. No existing reviewer or catalog gate is lifted.

## Implementation boundaries

- Add `src/relaymind/evaluation/asset_automated_host.py` and a closed schema for
  protocol/decision/record validation. Reject undeclared fields, unknown
  candidate IDs, mismatched content hashes and non-offered candidates.
- Add a new executor script under `evaluation/assets/automated-host-277/`.
  Reuse the existing bounded host process runner, token gate, run journal,
  capture writer, bundle support and judge engine rather than copying them.
  Extract reusable helpers only where the current cohort script cannot be
  imported safely. Historical entry points retain their current behavior.
- Create recommendations through the ordinary bearer API with expected scope;
  never provide an action credential to the driver. Poll with a fixed bound.
  The decision model sees only the task and public candidate projection, not
  held-out labels, rubric answers, other arms' outputs or credentials.
- Accept exactly one bounded structured proposal or abstention. Invalid output
  is a recorded failure; any retry is frozen and counted. A proposal creates
  no product choice receipt. Capture recommendation ID, actual candidate
  bytes, decision request/response and the exact resulting host prompt.
- Enclose proposal metadata as untrusted data in a frozen prompt template.
  Preserve the original task prompt separately and bind both hashes. No
  file path reads, tool execution or instruction loading follows a proposal.
- Count every driver and host generation in the recommendations row, including
  failed/retried calls. Preserve per-model usage rather than assuming the
  driver's tokens are free. Bound judge cost separately and disclose it.
- Freeze one equal per-generation deadline across all arms and enforce it at
  each actual host/provider boundary. Record row and total budgets explicitly.
  Do not silently raise the existing 30-second gateway bound.
- Run in a fresh private directory; store journal, token ledger, raw evidence
  and answers outside disposable work. Flush each capture before a row is
  marked complete. Resume never reruns a started but unrecorded row.
- Rebuild and bind actual package bytes under the new freeze. Changing the
  plugin means the historical `b066ae27` prefix is not a substitute for a new
  full SHA-256 pin. Verify live native inventory or keep that binding unverified.

## Tests and review

Write failing RM/AC-labelled tests first for forged candidate decisions,
malformed/oversized outputs, stale recommendation state, abstention, no action
credential use, missing usage, driver retry accounting, budget exhaustion,
interruption and durable evidence retention. Verify old owner_run reports and
person-action guards still reject unsupported claims.

Test preregistration binding changes, mutation of protocol/driver prompts,
missing blind judgments, fabricated human review fields, development-catalog
qualification, and exact conservation of 300 planned rows. Model doubles test
the executor; only actual captured calls establish a live result. Review the
whole branch, fix important findings, run the full checks, then open a PR with
no attribution trailers and no new issues.

## Run and completion conditions

Use a small separately labelled smoke dataset first to validate plumbing; it
must not alter the frozen 100-task cohort or be reported as its result. The
full run needs working provider credentials, the pinned host and package,
reconstructed private catalog and explicit finite token limits (the previous
issue estimated about 13M tokens plus 0.3M for judging; driver calls add cost).
These are prerequisites, not evidence that the run has happened.

Close revised #277 only after all 100 paired tasks are represented, every
actual outcome and cost is retained, judging and grading finish, and the
report and automated record are committed. Report metric failures as failures;
completion of the experiment does not require inventing a passing metric.
Keep #280's independent qualification outcome explicit. No follow-up issue is
needed; remaining product acceptance limits stay in the existing roadmap.

## Implemented preparation command

`scripts/asset_automated_host.py` accepts protocol, source tasks/lock and new
tasks/freeze documents, rejects changed original task semantics, and writes an
exclusive durable preregistration. It performs no network access. See
`scripts/README.md` for the complete invocation. This is preparation tooling,
not an executable acceptance run.

## Next executor implementation boundary

Reuse `Budget`, `RequestGate` and `run_bounded` from the existing #260 harness.
The proposal and host requests must share one row reservation and gate, opened
before the first proposal call and settled only after all capture writes.
Refactor or compose the row lifecycle instead of bypassing `Run.execute`'s
current recommendation refusal. Its `setup` currently starts a gateway with
recommendations disabled and the default external deadline, so the new setup
must realize and verify a separately frozen read-only recommendation gateway
configuration. The proxy's socket timeout is insufficient as a total
generation deadline; each upstream request needs an owned process that is
terminated at the frozen wall-clock limit, including trickle responses.
Missing or unparsable usage remains unknown in the report while the ledger
retains the conservative admitted charge. Capture raw model responses before
parsing proposals so invalid output never erases a paid attempt.
