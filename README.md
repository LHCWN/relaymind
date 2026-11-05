# RelayMind

**Get more from your models. Find the right skills and tools for the task.**

RelayMind is a local model gateway and skill/tool recommendation service. It
helps you work with a growing asset library: discover relevant capabilities,
compare candidates, and choose what to use. It also routes managed tasks between
local and external models, keeping completed work when a task needs to move.

## Why use RelayMind?

- **Find useful capabilities in your library.** Search skills and MCP tool
  metadata with English, Simplified Chinese or mixed-language requests. Bring
  local skills, import tool metadata, or use a qualified source profile.
- **Keep control when the match is uncertain.** Low-confidence recommendations
  ask you to choose. You can select a candidate, decline every option, or cancel.
  Recommendations never install packages, execute tools or approve changes.
- **Use local compute when it helps.** Run bounded work locally when the model
  is suitable; hand off to your configured external model when needed. An
  external-only setup also works.
- **Avoid losing work between models.** Carry completed tool results and the
  remaining budget into a handoff. Optional prefetch and multiple read-only
  calls can reduce model round trips.
- **See what happened.** Inspect selection, routing, tool outcomes, timing and
  reported usage. Unknown costs stay unknown; predictions are labeled.

These are two distinct interfaces: recommendations help you choose assets;
the managed gateway executes its configured demo skills under gateway rules.
A recommended asset does not automatically become an executable gateway skill.
See the [interface guide](docs/asset-selection.md#choose-the-right-interface).

## Choose your starting point

| You want to… | Start here |
| --- | --- |
| Find skills and tools in your own library | [Recommendation setup](docs/asset-selection.md) · [简体中文](docs/asset-selection.zh-CN.md) |
| Use RelayMind from OpenClaw | [OpenClaw setup](integrations/openclaw/README.md#operator-guide-145) |
| Connect an OpenAI-compatible client | [Gateway quickstart below](#install) |
| Plug in your own recommendation algorithm | [Selector v2 guide](docs/ASSET_BENCHMARK.md#the-skilltool-v2-boundary) |
| Compare selectors on the same catalog | [Source-backed benchmark](docs/SOURCE_BENCHMARK.md) |

## Works with OpenClaw

The included provider plugin supports **OpenClaw 2026.10.7**. Select
`relaymind/relaymind-client-tools` to use the gateway for inference. OpenClaw
keeps ownership of sessions, tool execution, permissions and approvals.

The optional recommendation panel supports discovery, explicit choices,
feedback and qualified native skill selection. Selection is distinct from
observed instruction use; loaded skill use and MCP execution have separate
gates. See the panel guide in [English](integrations/openclaw/README.md#native-recommendations-and-explicit-actions)
or [简体中文](integrations/openclaw/README.zh-CN.md).

The [installation guide](integrations/openclaw/README.md#operator-guide-145)
owns the pinned Node requirements, packaging, token setup and configuration.

## Scope and evidence

RelayMind is a single-user prototype with explicit catalog, context and execution
limits. Its [recorded development benchmarks](docs/SOURCE_BENCHMARK.md#recorded-development-results)
show the tested catalog sizes, misses and limitations. They do not establish
unlimited scale or guarantee better answers, lower cost or lower latency.

The gateway ships sandbox demo skills and tools. Its Chat Completions endpoint
implements the [documented OpenAI-compatible subset](docs/SPECIFICATION.md#7-public-api-contract).
Use the [roadmap](docs/ROADMAP.md) for current implementation and qualification
status, and the [evidence record](docs/EVIDENCE.md) for measured results.

## Install

You need Python 3.12 or newer on macOS (Apple Silicon) or Windows
(x86-64). No WSL or Docker.

```bash
git clone https://github.com/LHCWN/relaymind.git
cd relaymind
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

On Windows PowerShell, create the environment with `python -m venv .venv`
and activate it with `.\.venv\Scripts\Activate.ps1`.

Then install the local runtime (llama.cpp) and download the model
(Qwen3-0.6B, about 0.6 GB) as described in
[`models/README.md`](models/README.md). The local model is optional: a
gateway with only an external model works too (see "Without a local
model" below).

## Configure

Copy [`relaymind.example.toml`](relaymind.example.toml) to
`relaymind.toml`. The example works as it is for the local model and
the demo skills. To add an external model, fill in the
`[external_model]` table (base URL, model name, context limit) and put
the API key in an environment variable, never in the file:

macOS / Linux:

```bash
export RELAYMIND_EXTERNAL_API_KEY="your-provider-key"
```

Windows PowerShell:

```powershell
$env:RELAYMIND_EXTERNAL_API_KEY = "your-provider-key"
```

The limits that bound each run (time, model generations, tool calls)
have defaults. If the local model is slow on your machine, raise them in
the `[budgets]` table; the example file shows the keys, and every run
records the values it used.

Without an external model, a task that needs one ends with a clear
`needs_external` error.

### Without a local model

Leave out the `[local_model]` table and fill in `[external_model]`. The
gateway then picks the skill without a model call (the `example_match`
selector), asks for a missing service name itself, waits for your
approval before a change, and sends every generation to the external
model. Only `auto` and `external_only` can be used. With a local model,
the gateway also checks at startup whether it answers and whether it is
fast enough for the `[budgets]` stage limit; when it is not, one printed
line says so and `auto` uses the external model the same way. Set
`detect = false` in a `[placement]` table to skip that check. Every setting is described in
[`docs/SPECIFICATION.md`](docs/SPECIFICATION.md), section 14.1.

### Per-prompt guidance

The optional [guidance](docs/guidance.md) feature shows an OpenClaw model only the few
skills and tools one prompt needs, and can limit it to them. It works with no index, on Chinese
prompts over English skills, and with no API key; a cheap model tells similar skills apart. Any
third-party algorithm can replace its selection and be compared with the baseline on precision and
recall.

### Loopback HTTP selector

This is managed selection v1: it covers skills only and drives managed runs, not OpenClaw
traffic. For skills and MCP tools, see [guidance](docs/guidance.md).
[RM28](docs/SPECIFICATION.md#41-routing-and-local-autonomy) permits a selector running
in another local process; [section 14.3](docs/SPECIFICATION.md#143-loopback-http-selector-contract-spec-040)
of the specification holds the contract. Start the deterministic standard-library
example in another terminal:

```bash
python scripts/example_selector.py --port 8051
```

Configure its exact identity in the launch file:

```toml
[selection]
chain = [{ transport = "http", name = "example_keyword", version = "1", url = "http://127.0.0.1:8051/select" }, "example_match"]
timeout_seconds = 2
```

The gateway sends one redacted [SelectionRequest](src/relaymind/selection/schemas/selection_request.v1.json)
and validates the [SelectionResult](src/relaymind/selection/schemas/selection_result.v1.json).
Only plain `http` loopback endpoints are accepted; credentials, query strings, redirects
and environment proxies are excluded. Configuration needs no selector connection.
Bump the configured version when selector behavior changes. The gateway admits
proposals and retains its permission, identifier, tool and confirmation checks;
a selector never executes a tool or grants permission (RM28).

The keyword example has no model and makes no API calls. It demonstrates the
transport without claiming useful selection accuracy. HTTP attempts and their
nullable model-call self-reports are labeled separately in traces and evaluation;
those reports never verify total external inference cost. The same production
transport can be benchmarked with explicit `--selector-name example_keyword
--selector-version 1`; see [scripts/README.md](scripts/README.md). The
[87-case keyword HTTP capture](docs/EVIDENCE.md#2026-10-24--m8-keyword-http-selector-apple-m1)
records the actual scores, transport timings and limitations separately from the
built-in selector evidence.

## Start

Start the local runtime as
[`models/README.md`](models/README.md#start-the-runtime) shows, then
the gateway:

```bash
relaymind serve --config relaymind.toml
```

The gateway checks the configuration first and stops with one clear
message if something is wrong. Then it prints its address and a
one-time token:

```
Serving on http://127.0.0.1:8080
Application token: <token>
```

Keep the token in `RELAYMIND_TOKEN`; every request needs it.

## Use

For the Python example, install the client SDK separately:

```bash
python -m pip install openai
```

Use the model name `relaymind`, which means "route it for me":

```python
import os
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key=os.environ["RELAYMIND_TOKEN"])
reply = client.chat.completions.create(
    model="relaymind",
    messages=[{"role": "user", "content": "Is staging-api healthy right now?"}],
)
print(reply.choices[0].message.content)
```

Or with curl:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" -H "Content-Type: application/json" \
  -d '{"model": "relaymind", "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}]}'
```

- **Choose where it runs** with the header `X-RelayMind-Execution-Mode`:
  `auto` (default), `local_only`, or `external_only`.
- **Missing details** come back as a question; send a new request with
  the answer.
- **Changes** wait for approval in the trace UI; a "yes" in the chat
  is not an approval.
- **See every run** at `http://127.0.0.1:8080/ui` (enter the token).
  The `X-RelayMind-Run-ID` response header names the run.
- **Progress while a streamed request works.** By default the stream
  starts when the run has finished. Set `early_headers = true` in the
  `[performance]` table and a streamed request gets its headers at once,
  with `X-RelayMind-Events-URL` naming the run's event stream (open it with
  the same token to follow the run), then a keep-alive comment while it
  works. No answer text is sent before the answer is ready.
- **Verified live answers.** Set `answer_stream = "live"` in `[performance]`
  to publish eligible managed answer statements as they pass the skill gate.
  A late failure ends the stream without replacement text. Buffered remains
  the default while provider corpus parity is pending; non-streamed chat and
  client-owned calls stay buffered.
- **Fewer round trips.** In the same `[performance]` table,
  `calls_per_generation = "many"` lets the model ask for several read-only
  tools in one step, and `prefetch = true` runs the read-only calls a skill
  declares (when your request names the service) before the model starts.
  Both are off by default; see `relaymind.example.toml` and
  `skills/README.md`.
- **Warm-up at startup.** The gateway prepares the local model before using it.
  With recommendations enabled, lexical service can become available while
  model warm-up continues; local inference readiness stays fenced until it
  finishes. Configure warm-up and detection in `[placement]`; see the
  [launcher reference](docs/SPECIFICATION.md#141-operator-launcher).

## Documentation and contributing

| Need | Source |
| --- | --- |
| Required behavior and acceptance criteria | [Specification](docs/SPECIFICATION.md) |
| Current implementation and remaining gates | [Roadmap](docs/ROADMAP.md) |
| Measured results and their limitations | [Evidence](docs/EVIDENCE.md) |
| Code layout and ownership | [Architecture](docs/ARCHITECTURE.md) |
| Development setup and checks | [Contributing](CONTRIBUTING.md), [contributor rules](AGENTS.md) |
| Manual and automated verification | [Testing](docs/TESTING.md) |

Dated release and design records preserve historical context and do not override
these sources.

## License

MIT. See [LICENSE](LICENSE).
