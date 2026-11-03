# Automated paired host experiment (#277)

RM16, RM35, RM37, RM40–RM44; AC28, AC31–AC36; specification 12.6.7.

These inputs retain all 100 original tasks and their rubrics from
`../owner-run-260/`. The new wrappers bind the automated recommendation arm,
current plugin package, current native skill inventory and equal generation
limits. The catalog remains the original development catalog. This experiment
does not claim person-driven choices, loaded skill qualification, or full M10
acceptance.

## Frozen inputs

`protocol.json` pins the proposal model, three success judges, two separate
review contexts, prompt digests and token/time limits. `configs/budget.json`
additionally records host-turn, output and context limits. `host-freeze.json`
and `host-tasks.json` bind those bytes; `preregistration.json` is an offline
binding check, not evidence of a git commit or an executed run.

The driver and judges use `glm-5.3-flash`. Both postrun review contexts use
separate OpenCode 2 sessions with `opencode-go/glm-5.3-flash`, temperature 0,
so each can inspect the private original evidence using tools. Same-model
reviews remain explicitly machine reviews, not independent human approval.

Execution has a 13,000,000-token cap, 250,000-token row reservations, a
120-second wall deadline for each upstream generation and a 600-second host
turn limit. Output is capped at 8,192 tokens per execution request; context
is 128,000. Judging has a separate shared 300,000-token ledger. Reservations
include proposal calls and survive crashes or unknown usage. No silent budget
increase or rerun of a started host row is permitted.

## Future execution budgets

The completed experiment retains its original v1 `configs/budget.json`, freeze,
tasks and source commits. It is not replayed or silently upgraded. That harness
used fixed control limits (10 seconds for recommendation polling and each
control request, 60 seconds for readiness) outside its frozen budget document.
This omission is a limitation of the historical runner, not a newly measured
result or a change to the retained outcomes.

The current runner requires `relaymind.automated_host_task_budget.v2` for any
new execution. `budget.v2.example.json` shows the explicit positive finite
`recommendation_poll_seconds`, `control_request_seconds` and `readiness_seconds`
fields in addition to the existing limits. Use a separate configuration directory,
bind its budget digest into a **new** freeze and task wrapper, and commit that
preregistration before dispatch. Do not edit this experiment's frozen inputs.
Every new row's `automated-dispatch.json` records the complete frozen budget.
Polling waits within that original deadline; timeout/error paths retain the last
observed recommendation view. Model limits and reservations are unchanged.

## Execution workflow

1. Verify the original private release against the committed source lock.
   Use Python 3.12 / Unicode 15.0, matching the frozen release. Materialize
   the three exact metadata import manifests and their hash-verified originals
   in a fresh private catalog directory. Do not symlink private inputs.
2. Review the runner and transport, run tests and commit the frozen inputs
   and code. Record that commit as the preregistration. The runner rejects
   uncommitted or changed input bytes before setup or model dispatch.
3. Read the provider key into the process environment from private operator
   storage. Use the standard Z.ai base URL `https://api.z.ai/api/paas/v4/`.
   Never put a credential in a command argument or a checked-in file.
4. Run `run.py --help` for its explicit paths and limits. Keep the journal,
   ledger, environment and evidence outside disposable work. Supply the
   committed protocol, source tasks, new tasks/freeze/configs, original private
   lock/release, verified package, pinned OpenClaw executable, production
   catalog TOML and driver prompt. Supply the preregistration commit and
   exactly the frozen caps. The shared gateway is read-only for recommendation
   proposals; no direct-action token or choice/approval route is used.
5. Preserve the report and every raw capture. Generate blind items and their
   private map through `judge.py`; commit the map-digest/item binding before
   judging. Use the same judging ledger across both judges and adjudication.
   Open the map only when both judgments and adjudication are complete.
6. Grade the host report without inventing legacy human qualification. Bind
   protocol, preregistration, actual dispatch time, captures, blind judgments
   and separate reviewer outputs in the automated experiment record. Resolve
   material findings in this issue and PR, without opening follow-up issues.

The runner passes only actual offered metadata as untrusted context. It does
not load skill instructions, invoke an MCP tool, or change production manual
selection. Missing or failed evidence stays missing or failed. The execution
status remains unmeasured until a complete report and reviews are recorded.
