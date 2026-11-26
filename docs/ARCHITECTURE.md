# Architecture

This document maps the components defined in
[`SPECIFICATION.md`](SPECIFICATION.md) section 3 to the code layout. The
spec is the source of truth; this page only explains where things live.
Implementation and qualification status belong to the [roadmap](ROADMAP.md).
Follow the linked operator guides for commands.

## Component map

| Component | Directory | Main files | Responsibility |
| --- | --- | --- | --- |
| API | `src/relaymind/api/` | `app.py`, `chat.py`, `chat_request.py`, `native.py`, `recommendations.py`, `guidance.py`, `user_actions.py`, `auth.py`, `errors.py`, `idempotency.py`, `server.py` | Validate requests, authenticate callers, expose compatible chat, native run, recommendation, user-action, and guidance APIs, and map errors. `server.py` binds to loopback. |
| Orchestrator | `src/relaymind/orchestrator/` | `local_loop.py`, `run_state.py`, `router.py`, `route_decision.py`, `budgets.py` | Admit model proposals, enforce budgets and identifier provenance, run the managed loop, and own answer publication. |
| Handoff and client-owned inference | `src/relaymind/orchestrator/` | `handoff.py`, `client_loop.py` | Transfer completed work within the remaining budget; return client-owned tool proposals without executing them. |
| Lifecycle and performance | `src/relaymind/orchestrator/` | `recovery.py`, `warmup.py`, `local_status.py`, `placement.py`, `performance.py`, `prefetch.py`, `routing_cache.py`, `workflows.py` | Recover interrupted runs, assess local availability, predict placement, and implement optional optimizations and the fixed parallel workflow. Predictions never change budgets. |
| Managed skill selection (v1) | `src/relaymind/selection/` | `contract.py`, `catalog.py`, `chain.py`, `admission.py`, `model_router.py`, `example_match.py`, `http.py`, `settings.py` | Build the offered skill catalog, obtain a proposal, then admit it through gateway checks. HTTP selection uses the loopback transport. |
| Model adapters | `src/relaymind/adapters/` | All | Normalize provider requests, streamed events, usage and timing. Adapters propose calls; they never execute tools. |
| Skill registry | `src/relaymind/skills/` | `registry.py`, `cache.py`, `predicates.py` | Validate packages and prefetch declarations, cache parsed content, load instructions, and verify completion criteria. |
| Tool executor | `src/relaymind/tools/` | `registry.py`, `executor.py`, `sandbox.py` | Validate and execute allowed calls; own operation IDs, approvals, retry/replay and side effects. Demo data is disposable. |
| Run store | `src/relaymind/store/` | All | Persist versioned state, trace events, approvals, tool outcomes, timing samples and outcome projections. Published schemas validate stored events. |
| UI | `src/relaymind/ui/` | `trace_view.py`, `render.py`, `routes.py`, `comparison.py`, `settings.py` | Render recorded evidence, collect explicit input, and compare runs over reset fixtures. It does not infer hidden reasoning. |
| Launcher | `src/relaymind/` | `launcher.py` | Validate TOML configuration, wire dependencies, recover runs, warm the local model and print the application token. See [configuration](../relaymind.example.toml). |
| OpenClaw setup helper | `src/relaymind/` | `openclaw_setup.py` | `relaymind openclaw install`, `token` and `guidance`: check their inputs and run the host's own install, configure, enable, token and MCP list commands. `guidance` also writes the guidance plugin's tool list. Never starts the gateway or prints the token. |
| Shared boundaries | `src/relaymind/` | `canonical.py`, `schema_util.py`, `redaction.py`, `identifiers.py`, `stats.py` | Canonical hashes, schema validation, configured secret redaction, RM05 identifier provenance helpers, and adapter timing exports. |
| Asset catalog | `src/relaymind/assets/`, `src/relaymind/store/` | All of `assets/`; `asset_catalog.py` in `store/` | Discover and publish metadata, enforce eligibility, schedule refresh, and rebuild lexical/semantic indexes. |
| Asset recommendation selection (v2) | `src/relaymind/selection/` | `asset_*.py`, `adaptation_stage.py` | Rank offered skills and independent MCP tools with separate selection, fallback, hybrid and adaptation evidence. This is distinct from managed skill selection v1. |
| Per-prompt guidance (RM45 to RM48) | `src/relaymind/guidance/`, `src/relaymind/api/guidance.py`, `integrations/openclaw/guidance-plugin/` | `pipeline.py`, `models.py`, `sync.py`, `feedback.py`, plugin `index.js` | Stateless and off by default. For one prompt and the list the host sends, word match, optional keywords and picker, or a third-party selector propose the few assets. A separate host plugin names them in one labeled hint and, only when the operator sets `limit=strict`, refuses what was offered and not selected. The gateway retains nothing and removes nothing. With a third-party selector it can also keep the selector's index in sync (`sync.py`, RM47) and, after a successful turn, tell the selector which offered assets the turn used (`feedback.py`, RM48). See [guidance guide](guidance.md). |
| Recommendation lifecycle | `src/relaymind/recommendations/` | All | Store recommendation requests, explicit choices, corrections and action receipts; enforce backend authority and private-file boundaries. A choice does not grant execution permission. |
| Evaluation | `src/relaymind/evaluation/`, `evaluation/` | All | Load frozen corpora and locks, grade trace-backed results, resolve qualification records and export comparisons. See [asset benchmark](ASSET_BENCHMARK.md). |
| OpenClaw integration | `integrations/openclaw/` | All | TypeScript provider and recommendation integration over public host APIs. The host owns sessions, tools and approvals. See its [operator guide](../integrations/openclaw/README.md). |

Supporting files:

- [Developer scripts](../scripts/README.md) index the launch, benchmark,
  qualification and diagnostic commands.
- [Demo skills](../skills/README.md) define package layout and the shipped
  sandbox skills. Runtime `SKILL.md` files are instructions, not general docs.
- [Models](../models/README.md) describes pinned manifests and local downloads;
  GGUF files are not committed.
- `tests/` contains deterministic tests and sandbox fixtures. Live capability
  tests are separate; see [Testing](TESTING.md).
- `docs/evidence/` contains dated records and original exports. Follow the
  [documentation rules](../CONTRIBUTING.md#documentation-changes) for preserved records.

## Managed execution and performance

The orchestrator owns both local and managed external stages. Its handoff
builder keeps completed tool results and completion criteria in the external
request. The client-owned alias is separate: it returns proposals for the caller
to execute and never takes ownership of host tools.

The specification defines the behavior of these components:
[execution boundaries](SPECIFICATION.md#43-tools-and-execution-boundaries),
caching (RM24), prefetch (RM29),
[early feedback](SPECIFICATION.md#73-streaming-and-early-feedback),
[gated live answers](SPECIFICATION.md#731-verified-live-publication-spec-041-rm12-rm20-rm32-ac19),
[launch settings](SPECIFICATION.md#141-operator-launcher) and
[placement](SPECIFICATION.md#142-placement-execution-contract-spec-039).

## Runtime shape

A single-user local service:

```
client ──► FastAPI (api) ──► orchestrator ──► adapters ──► llama.cpp server (local)
                                │                      └─► external provider (configured)
                                ├─► skills (registry)
                                ├─► tools (executor) ──► sandbox demo data service
                                └─► store (SQLite)
```

- The gateway binds to loopback and requires a generated application token.
- The operator starts the gateway with `relaymind serve --config <file>`
  (the launcher above, spec section 14.1). One TOML file wires the
  models, skills, sandbox, store, settings, policy, and UI data mode.
- The demo UI is served by the same FastAPI app under `/ui` when the
  operator passes `UiSettings`.
- Local inference runs in a separate llama.cpp server process, also on
  loopback.
- External credentials stay server-side (environment variables or OS
  credential store).

## Ownership boundaries

- The model produces **proposals**.
- The orchestrator decides whether a proposal is **admissible**.
- The executor owns **side effects**.
- The UI renders **evidence**.

No package may cross these lines. In particular: adapters never execute
tools, skills never execute anything, and the store never makes routing
decisions.

## Dependency direction

```
api ──► orchestrator ──► skills ──► tools ──► store
          │  │  │                    │
          │  │  │                    └─► adapters (ToolSchema)
          │  │  ├─► tools
          │  │  └─► store
          │  └────► adapters
          └───────► redaction
```

The core execution stack is strictly layered from top to bottom:
`api` (with sibling `ui`), `orchestrator`, `skills`, `tools`, then core `store`
(`run_store.py`, `trace_event.py`, `model_timings.py`, `outcomes.py`) and
`adapters` side by side, then the leaf modules (`canonical.py`, `schema_util.py`,
`redaction.py`).
These dependency rules are enforced by `tests/test_architecture_imports.py`.

Core stack rules:

- `canonical.py`, `schema_util.py`, and `redaction.py` are leaf modules that
  import no other RelayMind package.
- `adapters/` imports no other RelayMind package and must not depend on SQLite
  or `store/`.
- Core run-store modules (`store/__init__.py`, `store/run_store.py`,
  `store/trace_event.py`, `store/model_timings.py`, `store/outcomes.py`) import
  only leaf modules and core store modules, and must not depend on M10 `store/`
  modules, `adapters/`, or higher layers. `tools/`, `orchestrator/`, and `ui/`
  import only core `store/` modules (never M10 `store/` modules).
- `identifiers.py` holds the shared RM05 identifier provenance and schema
  helpers; it imports only `adapters/` (and `tools/registry.py` for type
  checking), so `skills/` never imports upward from `orchestrator/`.
  `orchestrator/identifiers.py` re-exports it for compatibility.
- `stats.py` imports only `adapters/base.py`.
- `tools/` imports `store/`, `adapters/base.py`, and leaf modules.
- `skills/` imports `tools/registry.py`, `identifiers.py`, and leaf modules.
- `orchestrator/` imports `skills/`, `tools/`, `store/`, `adapters/`,
  `selection/`, `identifiers.py`, and leaf modules; it never imports `api/`,
  `ui/`, `assets/`, `recommendations/`, `guidance/`, `evaluation/`, or
  `launcher.py`.

Named subsystems and explicit exceptions:

- **Managed skill selection v1 (`selection/`):** sits beside the orchestrator's
  routing modules rather than below the whole orchestrator. V1 `selection/`
  modules import `orchestrator/router.py`, `orchestrator/route_decision.py`,
  and `orchestrator/placement.py` (`PlacementDeclined` in `selection/chain.py`),
  `identifiers.py`, `skills/`, `tools/`, `adapters/`, and leaf modules (never
  `assets/`, `recommendations/`, or `store/`); `orchestrator/local_loop.py`
  and `orchestrator/routing_cache.py` import `selection/`. None of the
  orchestrator modules that `selection/` imports (directly or transitively
  within `orchestrator/`) imports `selection/` back.
- **Asset catalog, recommendations, and v2 selection (`assets/`,
  `recommendations/`, `selection/asset_*.py`, `selection/adaptation_stage.py`,
  and M10 `store/` modules):** form a dedicated subsystem built over
  `RunStore`'s SQLite connection rather than sitting beneath `tools/` or
  `orchestrator/`:
  - M10 store modules (`store/asset_catalog.py`, `store/asset_feedback.py`,
    `store/asset_remote_state.py`, `store/asset_vectors.py`,
    `store/index_job_event.py`, `store/recommendation_event.py`,
    `store/recommendations.py`) persist M10 domain records and import schema
    types, validators, and error definitions from `assets/`,
    `recommendations/`, and `selection/asset_*.py`.
  - `assets/` imports `store/` (for persistence), `tools/registry.py` (to
    project MCP tool schemas), `selection/` (`asset_contract.py`,
    `asset_lexical.py`, `http.py`, plus `assets/embedding_bundle.py` importing
    `relaymind.selection` to hash `asset_hybrid.py`, `asset_adaptation.py`,
    `adaptation_stage.py`, and `asset_chain.py`), `recommendations/service.py`
    (in `assets/index_lifecycle.py`, `TYPE_CHECKING`-only), and leaf modules.
  - `recommendations/` imports `assets/`, `selection/`, `store/`,
    `api/errors.py` (in `recommendations/errors.py`, to share the HTTP error
    mapping), and leaf modules.
  - V2 selection modules (`selection/asset_*.py` and
    `selection/adaptation_stage.py`) import `assets/`, `recommendations/`,
    `store/`, and leaf modules.
- **Per-prompt guidance (`guidance/`):** sits beside `recommendations/` and
  imports `adapters/`, `assets/catalog.py`, `recommendations/limits.py`,
  `selection/` (`asset_admission.py`, `asset_contract.py`, `asset_http.py`,
  `asset_lexical.py`, `http.py`), and leaf modules. It never imports
  `orchestrator/`, `store/`, `skills/`, `tools/`, `api/`, or `ui/`.
- **Demo UI (`ui/`):** sits beside `api/`. `api/app.py` installs UI routes, and
  `ui/` imports `api/`, `orchestrator/`, `store/`, and `adapters/`.
- **Composition roots and evaluation (`launcher.py`, `evaluation/`,
  `openclaw_setup.py`):** sit at the top to wire the server, run benchmarks,
  and configure OpenClaw. No runtime package below them imports them.

The demo UI handles trace rendering (`ui/trace_view.py`, `ui/render.py`),
route wiring (`ui/routes.py`), and sandbox comparison runs (`ui/comparison.py`).
A chat disconnect asks the orchestrator to cancel the run the request owns
(`request_cancel`), the same path as the native cancel (section 7.4).

Lower core layers must not import higher ones. The store must not depend on
adapters. Adapters must not depend on SQLite.

## Independent asset recommendations

Sources discover and atomically publish safe catalog metadata; the existing
worker owns derived indexing. A v2 selector receives the full eligible offer and
proposes a shortlist. The gateway owns budgets, admission and the separately
attributed lexical floor. The recommendation API stores lifecycle/evidence;
explicit choice does not execute an asset. Qualified host authority remains a
separate boundary. See [customer setup](asset-selection.md),
[v2 and preparation contracts](ASSET_BENCHMARK.md),
[source diagnostics](SOURCE_BENCHMARK.md) and [privacy ownership](asset-privacy-security.md).

## Qualified remote skill refresh (RM34/RM38/RM43)

`assets/remote_contracts.py` owns the reviewed installed
`github.relaymind-skills.v1` profile. Launch configuration names that contract;
it cannot supply a URL, import adapter code or self-qualify. Ordinary user
libraries still use local directories or explicit metadata imports.
`remote_transport.py` keeps one bounded HTTP exchange at a time inside the
existing drained capture worker. Native response waits close the CPU window;
parsing, hashing and publication reacquire its foreground gate. The transport
refuses a depleted request/byte budget before dispatch, sends optional bearer
credentials only to the fixed API origin, rejects redirects and records no
credential/reference or raw exception body in public evidence.

`remote_github.py` pins the branch's commit for a complete root/skills inventory,
then verifies every selected original, optional manifest and MIT notice against
Git blob length/ID and independent SHA-256. It shares ordinary skill projection
with local acquisition. Source versions are immutable commits; declared package
versions stay separate. Only a fully valid configured scope supplies explicit
source-owned removals to the existing atomic catalog/lexical publication.
Errors retain the last good generation and its truthful stale/last-success state.

`store/asset_remote_state.py` persists mandatory not-before UTC by installed
profile. Atomic poll reservation prevents a concurrent owner, renamed source ID
or explicit rescan from bypassing it. `AssetCatalogService` uses this state in
its existing due/delay queue, alongside ordinary source-ID backoff and configured
cadence. A blocked remote future does not spin or starve another eligible
source. There is no new worker, execution-slot acquisition or public cooldown
API. Restart repairs interrupted jobs and rebuilds lexical state from committed
metadata, preserving both source content and provider cooldown.

### Local semantic backend ownership

The optional section 6.1 artifact loader verifies bounded local originals and
exact runtime/device identities before a reviewed qualification decision.
`embedding_bundle.py` binds semantic configurations to effective launch identity;
the production qualification registry holds the two exact reviewed Linux CPU
identities for the pinned multilingual bundle. No supplied config/evidence flag,
model URL, environment default, arbitrary backend import or different build,
device or semantic config can activate it.

Hybrid query representations, document vectors and bounded correction similarity
use the same pinned local backend with separate inference observations. The
source/lexical worker also owns semantic rebuilds; remote hard cooldown applies
to its installed source profile and does not delay a derived-only rebuild.
Catalog/content changes invalidate vectors; deletion prunes them. Source job
identity binds both effective semantic configuration and refresh/profile/budget
policy. Native I/O is quiescent; native CPU construction/inference retains owned
windows and actual drain until completion. Cooperative targets do not imply an
unmeasured universal cancellation or resource bound.

Enabled adaptation accepts only eligible scoped explicit corrections, with
bounded history/votes and rank movement. It cannot add a missing candidate,
promote confidence or grant host authority. Reset/delete fence private buffers
before acknowledgment. A failed required stage retains known inference evidence
and refuses success. Artifact activation is limited to the reviewed identities above. Host authority
and final episode, quality and platform acceptance remain separate gates.
