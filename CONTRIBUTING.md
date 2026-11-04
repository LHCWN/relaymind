# Contributing to RelayMind

Thanks for your interest in contributing to **RelayMind**.

## Development prerequisites

- **Python 3.12+**
- **llama.cpp server** (optional — only needed for live local-model tests)

Clone and install:

```bash
git clone https://github.com/LHCWN/relaymind.git
cd relaymind
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows PowerShell, use `python -m venv .venv` and
`.\.venv\Scripts\Activate.ps1` instead of the POSIX activation command.
For browser tests, run `playwright install chromium`. OpenClaw tests also
need the pinned Node environment and package setup in the
[integration guide](integrations/openclaw/README.md).

## Commands

| Action | Command |
|--------|---------|
| Run full test suite | `pytest -n auto --dist loadfile` |
| Run a single test file | `pytest tests/test_<name>.py` |
| Run a specific test | `pytest tests/test_<name>.py::test_case` |
| Run live model tests | `pytest -m live` (needs `llama-server` and the model; see `models/README.md`) |
| Lint | `ruff check .` |
| Check formatting | `ruff format --check .` |
| Format | `ruff format .` |

Manual testing from a terminal (JSON requests on macOS and Windows) is
described in [`docs/TESTING.md`](docs/TESTING.md).

## How we work: SDD and TDD

This project uses Spec-Driven Development and Test-Driven Development.

- The spec ([`docs/SPECIFICATION.md`](docs/SPECIFICATION.md)) is the single
  source of truth. Every task and test references its requirement IDs
  from the current specification; retired IDs must not be reused.
- Behavior changes need a spec revision **before** code changes.
  Implementation-only refactoring does not.
- Write a failing test first, then implement until it passes.

See [`AGENTS.md`](AGENTS.md) for the full workflow and invariants, and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the repository map.

## Tests and fixtures

- `tests/` holds the deterministic suite. It uses model doubles and must
  not depend on live model output.
- `tests/fixtures/` holds disposable sandbox demo data. Tests must never
  touch a production resource.
- Live model tests are marked separately and are not part of the default
  run.

## Formatting and style

- Run `ruff format .` before committing.
- Run `ruff check .` and fix all warnings.
- Use type hints on public functions.
- Write docs, comments, and messages in simple English.

## Pull request expectations

- Keep PRs focused — one logical change per PR.
- Name the requirement IDs (RM / AC) the change covers in the description.
- All CI checks must pass (`pytest`, `ruff check`, `ruff format --check`).
- If the change touches API behavior, tool ownership, approvals, platforms,
  or evaluation definitions, link the spec revision that allows it.

## Documentation changes

Keep documentation in version control and review it with the implementation.
Use the [documentation map](README.md#documentation-and-contributing) to choose
one canonical home for each fact. Other pages should link to it, using a short
summary only when readers need context.

- Separate quickstarts, task guides, reference contracts and design explanations.
  Follow the reader's task rather than the order in which issues were implemented.
- State prerequisites before commands. Mark shell/platform differences, use
  copyable examples and describe the expected result.
- Preserve existing link anchors when shortening a page. Verify relative links,
  referenced files and documented command options after editing.
- Update English and Chinese guides together when supported behavior changes;
  link shared reference details instead of maintaining two copies of contracts.
- Never rewrite frozen prompts, runtime skill instructions, captured output,
  licenses or hash-bound originals as editorial cleanup. Add a current pointer
  outside the preserved record when historical context needs clarification.
- Use plain language, descriptive headings and link text, numbered procedures,
  and explicit dates for historical claims. Avoid unsupported scale, quality or
  performance promises.

These conventions use the reader-oriented organization of
[Diátaxis](https://diataxis.fr/) and the
[Google developer documentation style guide](https://developers.google.com/style/highlights).
They supplement the existing specification change-control rules in [AGENTS.md](AGENTS.md).

## Reporting issues

Open an issue with:

- A minimal reproduction (request payload or test case).
- Expected vs. actual behavior.
- The requirement ID involved, if known.

## Code of conduct

This project is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you are expected to uphold it.
