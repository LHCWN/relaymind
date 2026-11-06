# AGENTS.md — Contributor guide for RelayMind

This file is for AI agents and human contributors working on the RelayMind
codebase.

## The two rules

1. **The spec is the single source of truth.**
   [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) defines what RelayMind
   does. Every implementation task and every acceptance test must reference
   requirement IDs from it (RM01–RM46, AC01–AC40; RM30 and AC23 are retired;
   RM33 and AC24 specify the M9 OpenClaw integration, added in 0.47 and
   revised in 0.49 and 0.50; RM34–RM44 and AC25–AC37 define M10
   recommendations, added in 0.51 and revised through 0.68; RM45, RM46 and AC38–AC40 define per-prompt guidance,
   added in 0.69; the spec is at 0.69).
   Sections 12.6.6–12.6.7 define recorded automated qualification review and
   the separate automated host experiment. Implementation status
   is recorded in docs/ROADMAP.md. If code and spec disagree,
   the spec wins. If the spec is wrong, revise the spec first, then change
   the code.
2. **Tests come before code.** Write a failing test that captures the
   requirement, watch it fail, then implement until it passes. Do not merge
   implementation code without tests.

## Repository map

[Architecture](docs/ARCHITECTURE.md) owns the component and source-file map.
[Roadmap](docs/ROADMAP.md) owns implementation and qualification status;
[Evidence](docs/EVIDENCE.md) links the dated measurements. Use those documents
instead of copying milestone status into this contributor guide.

For task-specific instructions, use [testing](docs/TESTING.md),
[developer scripts](scripts/README.md), [skill packages](skills/README.md),
[model setup](models/README.md), and the [OpenClaw integration](integrations/openclaw/README.md).
M10 operator setup starts with [asset selection](docs/asset-selection.md);
its contracts remain in specification sections 4.6–14.5.

## Ownership boundaries (do not blur these)

- The **model** produces proposals. It never executes anything.
- The **orchestrator** decides whether a proposal is admissible.
- The **executor** owns side effects.
- The **UI** renders evidence. It does not invent or infer hidden reasoning.

See spec section 3 for the full component contract.

## Invariants

1. **Local-first (RM01).** A request handled locally makes zero external
   inference calls. The trace must prove it.
2. **Bounded execution (RM03).** Every budget (generations, tool calls,
   time) is a configuration value recorded in each run. Never raise a budget
   silently.
3. **No invented identifiers (RM05).** Resource IDs come from the request,
   conversation context, or tool results. Never guess one to make a tool
   call possible.
4. **Validated tool calls only (RM09).** Validate full tool name and
   arguments before execution. Partial streamed JSON never executes.
5. **Confirmation for mutations (RM11).** Mutating demo actions need a
   fresh, single-use confirmation bound to run, tool, arguments, target, and
   state version.
6. **Tool output is data (RM12).** It never overrides gateway rules or
   grants permissions. Redact configured secret fields in logs and traces.
7. **Atomic state transitions (RM17).** Every transition checks the run
   state version. Stale versions get HTTP 409. Terminal states never
   restart.
8. **Append-only answers (RM20).** Once answer streaming starts, ownership
   is fixed. Never silently replace streamed text with another model's
   output.
9. **Honest evidence (RM16, section 12).** Never report an unavailable
   service as healthy, a draft as a log-backed diagnosis, or a schema
   example as a measured result.
10. **A selector proposes, the gateway admits (RM28).** A selector,
    or a prefetch declaration never executes anything, grants a
    permission, or skips a clarification or a confirmation.
11. **Predictions are labeled (RM27).** A placement prediction is
    recorded next to the actual duration and never reported as a
    measurement; the placement policy never changes a budget.
12. **Nothing unverified reaches a client (RM32).** A streamed
    statement is forwarded only after the gateway checked it; a late
    failure ends the stream, never replaces text.

## Development workflow

1. Pick a task. Confirm which RM requirements it implements.
2. If the task changes observable behavior not covered by the spec, stop
   and propose a spec revision first (see "Change control" below).
3. Write failing tests that reference the RM / AC IDs in a comment or
   docstring.
4. Implement until tests pass.
5. Run the full check suite before committing:

```bash
pytest -n auto --dist loadfile
ruff check .
ruff format --check .
```

## Commands

[CONTRIBUTING.md](CONTRIBUTING.md#development-prerequisites) contains environment
setup and the command reference. The full pre-commit checks are listed above.

## Testing policy

- **Deterministic tests use model doubles.** Orchestration, state,
  authorization, and API contract tests must not depend on live model
  output (spec section 13).
- **Live model tests are separate.** Capability tests with real models are
  marked `live` and excluded from the default run and CI (`pytest -m live`
  runs them). Passing doubles alone does not satisfy live-demo
  requirements.
- **Fixtures are sandboxes.** `tests/fixtures/` holds disposable demo data
  only. Nothing in tests may touch a production resource.
- **Bilingual coverage.** Demo skills carry paired English and Chinese
  examples; the evaluation corpus keeps both languages (spec section 12.2).

## Change control (SDD)

Changes to tool ownership, approval rules, API behavior, supported
platforms, or evaluation definitions require a specification revision
**before** implementation changes. Implementation-only refactoring may keep
the spec unchanged when observable behavior is unchanged (spec section 14).

Spec revisions bump the version line at the top of
`docs/SPECIFICATION.md` and are recorded in `CHANGELOG.md`.

## Style

- Python 3.12+, type hints on public functions.
- Simple English in docs, comments, and messages.
- Keep PRs focused: one logical change per PR, with the RM / AC IDs it
  covers in the description.
