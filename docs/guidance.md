# Per-prompt guidance

[简体中文](guidance.zh-CN.md)

A user writes a prompt in OpenClaw. RelayMind proposes the few skills and tools that prompt needs, and
the model sees those instead of a long list. If you choose, OpenClaw can also be limited to them.
Requirements RM45 and RM46, specification section 7.7.

The gateway proposes only. It never runs anything or grants a permission. If it is slow or fails, the turn
goes on without a hint and without a limit, and OpenClaw's own search still works.

## What you need to know first

- **No waiting.** It needs no index and no model to answer. It reads the list the host sends with each
  prompt, so a skill you just installed counts on the next prompt.
- **Mixed languages.** Plain word match cannot connect a Chinese prompt to an English skill. A small
  local model fixes much of this by writing English keywords, with no API key.
- **The right set, not one skill.** A simple prompt gets one skill. A big workflow gets one for each of its tasks, usually 3 to 5 and sometimes more. The default limit is 8, and `max_picks` can go up to 20.
- **Similar skills.** A market often has many skills that do nearly the same job. A good choice needs a
  model that reads what sets them apart, so `picker = "external"` matters most here. When a prompt fits
  several and does not say which, the hint tells the model to ask the user.
- **Hide, or limit.** By default it hides the rest: the model can still load any skill through OpenClaw's
  search and read tools, so a wrong choice is not fatal. With the [strict limit](#limit-openclaw-to-the-selection)
  it also refuses them.

## Set it up

1. Turn it on in the gateway's launch file:

   ```toml
   [guidance]
   enabled = true
   keywords = "local"   # none, local (needs [local_model]) or external (needs [external_model])
   picker = "none"      # none, or external: a cheap model chooses the final few
   ```

   | Setup | Needs | How the final choice is made |
   | --- | --- | --- |
   | `keywords = "none"`, `picker = "none"` | Nothing | Up to 8 strong word matches; OpenClaw's model chooses |
   | `keywords = "local"`, `picker = "none"` | A local model | The same, and it also works across languages |
   | `keywords = "local"`, `picker = "external"` | A local model and an API key | A cheap model chooses the set the prompt needs |
   | `keywords = "external"`, `picker = "external"` | An API key | The same, with the best results and a little slower |
   | `primary = "http"` | Your selector | Your selector chooses |

   Keywords run only for a prompt with letters outside the Latin script, so English prompts stay fast. When
   the picker finds nothing, keywords run once more for any prompt, because the prompt may share no word
   with the right skill. A short follow-up such as "yes, do that", whose own words find nothing strong, is
   matched together with what the user said just before it.

2. Start the gateway from the repository root: `relaymind serve --config relaymind.toml`. It prints an
   application token. Put it in `RELAYMIND_APP_TOKEN` for the OpenClaw process.

3. Install the [guidance plugin](../integrations/openclaw/guidance-plugin/README.md) in OpenClaw, and set
   `skills.limits.maxSkillsInPrompt` to `0`.

   With many skills, also raise `skills.limits.maxSkillsLoadedPerSource` (default 200) and
   `maxCandidatesPerRoot` (default 300). OpenClaw does not load a skill past these caps, so it cannot be
   selected or found by search. The plugin logs a warning when a session reaches the first cap.

4. Check it. In OpenClaw, ask something, then look for `[RelayMind guidance, not from the user]` at the
   start of the prompt in the request. Or call the gateway yourself:

   ```bash
   curl -sS http://127.0.0.1:8080/relay/v1/guidance \
     -H "Authorization: Bearer $RELAYMIND_APP_TOKEN" -H 'Content-Type: application/json' \
     -d '{"schema_version":1,"task_text":"merge two pdf files",
          "assets":[{"name":"pdf-merge","kind":"skill","description":"Merge pdf documents"}]}'
   ```

   ```json
   {"schema_version":1,"decision":"picks","picks":[{"name":"pdf-merge","kind":"skill"}],
    "chosen_by":"word_match","mechanism":"word_match","degraded":false,"offered":1,"shortlist":1,
    "stages":[{"stage":"word_match","outcome":"ok","ms":0.1,"usage":null}]}
   ```

   `degraded: true` means a configured stage failed; `stages` says which one. Check this when results look
   poor: a keyword model on the wrong port quietly gives you plain word match.

## Limit OpenClaw to the selection

Set `limit` in the [plugin's configuration](../integrations/openclaw/guidance-plugin/README.md):

```json
{"baseUrl": "http://127.0.0.1:8080/v1", "limit": "strict"}
```

For each prompt the plugin then refuses, with a message that names what was selected:

- `skills_read` of a skill not selected;
- `skills_search`, which would bring the rest of the catalog into the context;
- a plain `read` inside the folder of a skill not selected, however the path is written;
- a call of a `toolsFile` tool not selected.

A skill or tool the session already used with success stays usable, so a follow-up keeps working. There is no limit when
the proposal failed, when nothing was selected, on a turn the user did not start, or in another session such
as a subagent's. A failure never blocks a turn.

Choose it knowing two things:

- A wrong choice now costs the right skill, because search cannot rescue it. Use it with a picker.
- It limits ordinary use. It is not a security wall: `exec`, or any tool that reaches the file system, can
  still read a skill, and a tool outside `toolsFile` is not limited. In OpenClaw's code mode, `skills.list()`
  shows every skill without a tool call, so no hook sees it. For a wall, also remove those tools, and code
  mode, with OpenClaw's own tool policy.

## Connect your own algorithm

The selection part is made to be replaced. Your algorithm takes the place of word match, keywords and picker,
and everything after it stays the same for any selector: the hint, the strict limit and the metrics.

Your algorithm is a local HTTP service that answers one request. The contract is published:
[request](../src/relaymind/selection/schemas/selection_request.v2.json),
[response](../src/relaymind/selection/schemas/selection_result.v2.json).
[`scripts/example_intent_selector.py`](../scripts/example_intent_selector.py) is a complete selector in about
200 lines, with no RelayMind imports. Copy it to start.

```bash
python scripts/example_intent_selector.py --port 8940
```

Without a model it is only word match, so it finds nothing for a Chinese prompt against English skills. Add
`--model-url`, `--model`, `--api-key-env` and, for a model that thinks by default, `--reasoning-effort low`.

```toml
[guidance]
enabled = true
primary = "http"                         # required: without it the URL is ignored
name = "example_intent"                  # must equal the name your result returns
version = "1"                            # raise it when behavior changes
url = "http://127.0.0.1:8940/select"
total_ms = 12000
```

The same selector also works for [recommendations](asset-selection.md#connect-a-team-selector): the table is
`[recommendations]`, and there you must raise `primary_ms` and `total_ms` from their 150 ms and 500 ms.

A request looks like this (shortened):

```json
{"schema_version":2,"recommendation_id":"7e46678a-d704-4027-b916-2c84de864101","task_id":"guidance:1",
 "task_text":"merge two pdf files","conversation":[],"scope":{"user_id":"guidance","workspace_id":"host"},
 "permissions":["assets.read"],"requested_kinds":["skill"],"selection_scope":"discovery",
 "catalog_revision":1,"host_snapshot_revision":null,"language_tags":["und"],"code_switched":false,
 "deadline_ms":7990,"candidate_limit":10,
 "catalog":[{"schema_version":1,"asset_id":"host:skill:pdf-merge","kind":"skill","source_id":"host",
   "source_locator":"host://skill/pdf-merge","source_version":null,"captured_at":"1970-01-01T00:00:00Z",
   "content_hash":"<sha256 of the description>","description":"Merge pdf documents","language_tags":[],
   "dependencies":[],"availability":"unknown","stale":false,
   "identity":{"package_id":"pdf-merge","package_version":null}}]}
```

Your answer must echo the IDs and offer only exact records:

```json
{"schema_version":2,"recommendation_id":"7e46678a-d704-4027-b916-2c84de864101","catalog_revision":1,
 "host_snapshot_revision":null,"selector":{"name":"example_intent","version":"1"},"mechanism":"my_rule",
 "outcome":"ranked","decision":"choice_required","index_revision":null,"elapsed_ms":3,"usage_claims":null,
 "candidates":[{"asset_id":"host:skill:pdf-merge","kind":"skill","content_hash":"<same as offered>",
   "source_version":null,"score":1.0,"score_kind":"my_score","evidence_refs":["/description"]}]}
```

The rules:

- Plain `http` on a loopback address, one POST per attempt. No redirects, no retries, no authentication.
- Answer HTTP 200 with `application/json`, at most 1 MiB. No duplicate JSON keys or non-finite numbers.
- `mechanism` is your own label. Do not use `lexical_overlap`, `gateway_lexical_fallback` or the built-in
  selector names; they are refused.
- If nothing fits, answer `"outcome":"no_match","decision":"no_match","candidates":[]`.
- A failure or an invalid answer falls back to word match with `degraded: true`, unless `fallback = false`.
- A selector proposes. The gateway admits only exact offered records, and nothing runs because of a pick.

Above 1,000 host assets your selector gets the best 1,000 by word match; the built-in stages take up to
20,000. A hosted service needs a small local process in front of it, because the URL must be on loopback.

## Add MCP tools

OpenClaw's Tool Search already keeps MCP tool schemas out of the request by default, so tools cost little
context. OpenClaw lets only official plugins read its tool list, so guidance gets tools from the plugin's
`toolsFile`, a JSON list of `{"name","description"}`.

For the recommendation catalog, tools arrive through a metadata import. `content_hash` is the SHA-256 of the
file at `original_path`, and `schema_hash` is the canonical digest of `input_schema`
([schema](../src/relaymind/assets/schemas/asset_metadata_import.v1.json)):

```json
{"schema_version":1,"source_id":"tools","source_version":null,"captured_at":"2026-11-05T00:00:00Z",
 "complete":false,"removed_asset_ids":[],
 "records":[{"original_path":"originals/get_weather.txt","record":{
   "asset_id":"demo:tool:get_weather","kind":"mcp_tool",
   "source_locator":"https://example.invalid/mcp/get_weather","source_version":null,
   "captured_at":"2026-11-05T00:00:00Z",
   "content_hash":"0000000000000000000000000000000000000000000000000000000000000000",
   "description":"Get the current weather for a city","language_tags":["en"],"dependencies":[],
   "identity":{"server_id":"demo","tool_name":"get_weather","exposed_name":"demo__get_weather",
     "input_schema":{"type":"object","properties":{"text":{"type":"string"}}},
     "schema_hash":"0000000000000000000000000000000000000000000000000000000000000000"}}}]}
```

```toml
[[assets.sources]]
source_id = "tools"
kind = "metadata_import"
path = "import/import.json"
```

## Compare against the baseline

The quickest comparison needs no setup beyond your selector. It uses the skills in
[`similar.v1.json`](../evaluation/assets/guidance-similar/similar.v1.json): families that do nearly the same
job and differ in one real way, with prompts in English and Chinese, follow-ups, prompts that fit a whole
family, complex requests that need several skills, and unrelated prompts.

```bash
python scripts/guidance_similar_benchmark.py --split holdout --arm word --output word.json
python scripts/guidance_similar_benchmark.py --split holdout --arm third_party \
  --selector-url http://127.0.0.1:8940/select --selector-name example_intent --output mine.json
python scripts/guidance_compare.py --baseline word=word.json --arm mine=mine.json --by-lane
```

| Metric | Meaning | Better |
| --- | --- | --- |
| First pick | The first pick is right | Higher |
| MRR | Mean of 1 / the rank of the first right pick, 0 when none | Higher |
| P@3 | Of the first 3 picks, the share that are right. Prompts with no pick are left out | Higher |
| R@3, R@8 | The first 3 or 8 picks hold a right answer | Higher |
| F1@3 | 2 x P@3 x R@3 / (P@3 + R@3) | Higher |
| Picks | Picks per prompt, as delivered | Lower |
| False picks | An unrelated prompt got any pick | Lower |
| Asked | A prompt that fits several assets got `decision: choose` | Higher |
| Offered | The share of those several assets that were offered | Higher |
| Set found | On a big workflow, the share of its needed set that was picked | Higher |
| All found | On a big workflow, the whole needed set was picked | Higher |
| Set exact | On a big workflow, of the picks, the share that are needed | Higher |
| Set picks | On a big workflow, picks per prompt | About the set's size |
| Failed | A stage or the selector failed | Lower |
| Wins/losses (p) | Prompts where only one side did better, with a sign test; repeated runs of a prompt count once | More wins |

Selection v2 has no `choose`, so a third-party selector shows 0% under Asked, and a report of the platform benchmark
shows "-". Failed counts a selector that did not answer itself, even when a fallback did. Develop on
`--split development` and report on `--split holdout`, which you should not tune against. The unrelated prompts and
the short follow-up phrases are the same in both splits, so those two lanes are not held out.

The same tool reads reports of the larger source benchmark. Compare on the same prompts through the same
gateway path. Build the data once, as in [SOURCE_BENCHMARK.md](SOURCE_BENCHMARK.md). Then, for each arm, run a
selector and:

```bash
python scripts/guidance_selector_server.py --port 8941 --name guidance_local --keywords local &
python scripts/source_asset_benchmark.py run --catalog catalog-v3/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v3.json --evidence-root catalog-v3 \
  --production my-arm-production.toml --track service --output my-arm.json
python scripts/source_asset_benchmark.py compare --left builtin.json --right my-arm.json --output cmp.json
python scripts/guidance_compare.py --baseline builtin=builtin.json --arm mine=my-arm.json
```

Two arms compare only with identical time limits and fallback. Set `total_ms`, `primary_ms` and `fallback`
equal in both production files, or `compare` answers `incompatible_development_runs`. In a report, an arm
whose `mechanism` ends in `_degraded` had a failed stage: do not read it as the arm you configured.

Whether the model then loads the right skill is a different question. `scripts/guidance_turn_benchmark.py` runs
real full turns through OpenClaw, the plugin, a gateway and an external model, with `--similar holdout` for similar
skills and `--tools` for MCP tools. Run the default arm twice: identical runs differ, and that difference is the
noise any improvement must beat.

Results so far are in [EVIDENCE.md](EVIDENCE.md#2026-11-05--per-prompt-guidance-development-runs-284). They are
development diagnostics: the expected answers are developer-authored, and they do not satisfy AC25 to AC37.
