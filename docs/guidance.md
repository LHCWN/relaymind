# Per-prompt guidance

[简体中文](guidance.zh-CN.md)

A user writes a prompt in OpenClaw. RelayMind proposes the few skills and tools that prompt needs, and
the model sees those instead of a long list. If you choose, OpenClaw can also be limited to them.
Requirements RM45 to RM47, specification section 7.7.

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
   | `keywords = "none"`, `picker = "none"` | Nothing more | Up to 8 strong word matches; OpenClaw's model chooses |
   | `keywords = "local"`, `picker = "none"` | A local model | The same, and it also works across languages |
   | `keywords = "local"`, `picker = "external"` | A local model and an API key | A cheap model chooses the set the prompt needs |
   | `keywords = "external"`, `picker = "external"` | An API key | The same, with the best results and a little slower |
   | `primary = "http"` | Your selector | Your selector chooses |

   The gateway needs one `[local_model]` or `[external_model]` table to start, even when guidance never calls it.

   Keywords run only for a prompt with letters outside the Latin script, so English prompts stay fast. When
   the picker finds nothing, keywords run once more for any prompt, because the prompt may share no word
   with the right skill. A short follow-up such as "yes, do that", whose own words find nothing strong, is
   matched together with what the user said just before it.

2. Start the gateway from the repository root: `relaymind serve --config relaymind.toml`. It prints an
   application token. Put it in `RELAYMIND_APP_TOKEN` for the OpenClaw process.

3. Set up OpenClaw with one command. It runs OpenClaw's own commands to install the
   [guidance plugin](../integrations/openclaw/guidance-plugin/README.md), allow its hook and set the gateway address.
   After checking that the plugin loaded, it sets `skills.limits.maxSkillsInPrompt` to `0`:

   ```bash
   relaymind openclaw guidance --tools-file ~/.relaymind/tools.json
   ```

   Leave out `--tools-file` to offer skills only. Add `--limit strict` for the
   [strict limit](#limit-openclaw-to-the-selection). An option you leave out keeps what an earlier run set; to stop
   offering tools, run `openclaw config unset plugins.entries.relaymind-guidance.config.toolsFile`.

   `maxSkillsInPrompt` applies to every agent of that OpenClaw. The plugin declares OpenClaw 2026.10.7 exactly, and the
   command refuses any other release. After you upgrade OpenClaw, the host may skip the plugin, so give the model its
   list back with `openclaw config unset skills.limits.maxSkillsInPrompt`.

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
   "content_hash":"<sha256 of SKILL.md>","description":"Merge pdf documents","language_tags":[],
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
- A failure, an invalid answer, `"outcome":"not_ready"` or `"outcome":"unavailable"` falls back to word match
  with `degraded: true`, unless `fallback = false`. A partial answer, with a `mechanism` ending in `:partial`, is not a failure; see
  [Keep your selector in sync](#keep-your-selector-in-sync).
- A selector proposes. The gateway admits only exact offered records, and nothing runs because of a pick.

Above 1,000 host assets your selector gets the best 1,000 by word match; the built-in stages take up to
20,000. A hosted service needs a small local process in front of it, because the URL must be on loopback.

## Prepare before the first prompt

The plugin sends the host's list with each prompt, so a selector with no index has nothing to prepare. One that
builds an index need not wait for the first prompt: OpenClaw prints the same skills with no session, using its own
discovery, precedence and caps, so do not walk the skill folders yourself.

```bash
openclaw skills list --eligible --json    # keep skills with modelVisible true; --agent <id> for one agent
openclaw skills info <name> --json        # filePath is the skill's SKILL.md, with the full body
```

A request carries no skill body, so read it from `filePath`. `source_locator` is only the label
`host://skill/<name>`, not a path. `skills list` prints no paths, so this takes one `skills info` call per skill,
a few seconds each: run it in the background.

Treat this as a warm-up. Each request's list decides what may be picked, and it can differ per agent and session.
Match an indexed record by `identity.package_id`, the skill's name. The plugin sends each skill's SHA-256, so a
skill's `content_hash` is the hash of its `SKILL.md` and changes with the body. A skill whose file is larger than
1 MiB has no such hash: its `content_hash` covers only the description as sent, shortened and redacted.

For MCP tools the request's `input_schema` is the placeholder `{"type":"object"}`, because OpenClaw's `mcp probe`
lists names only. A selector that needs real schemas asks the servers itself with `tools/list`; `openclaw mcp show`
prints how to reach them.

## Keep your selector in sync

A selector that indexes skill bodies can let the gateway tell it what changed (spec 0.72, RM47). Add
`sync_url = "http://127.0.0.1:8940/sync"` to the `[guidance]` table above. When your selector answers
`"outcome":"not_ready"`, the gateway falls back to word match for that turn and, after it answers, sends up to
three POSTs to that URL:

1. `{"schema_version":1,"operation":"find_missing","feeder_id":"guidance","assets":[{"asset_id","content_hash"}]}`
   with every offered record. Answer `{"schema_version":1,"operation":"find_missing","inference":"local",
   "missing":[...],"results":null}` with the pairs you have not indexed.
2. `{"schema_version":1,"operation":"upload","feeder_id":"guidance","assets":[{"record","content"}]}` with only
   those. `record` is the exact record of the select request. `content` is
   `{"kind":"file","path":"/abs/SKILL.md","size_bytes":812}` for a skill, and `null` for a tool or a skill with
   no file. Answer `"missing":null` and one result for each pair:
   `{"asset_id","content_hash","status":"accepted","reason":null}` once you have taken it for indexing, or
   `"status":"rejected"` with `"reason"` one of `hash_mismatch`, `size_mismatch`, `outside_roots`, `too_large`,
   `unreadable` or `invalid_record`.

Read the file yourself, only when its real path, with every symbolic link resolved, is under folders your
operator allowed, and check its size and SHA-256 against `size_bytes` and `content_hash`. Index each asset by its
exact `asset_id` and `content_hash`, and answer `not_ready` to a select that offers a pair you have not indexed.
Report a pair as missing when you have neither indexed it nor are indexing it, so a pair whose indexing failed is
missing again. Say `"inference":"remote"` if your embedding or
tag model is not on this machine: the gateway then sends no paths, unless the operator sets
`sync_allow_remote = true`. The gateway never reads or sends a skill body, and at most one sync runs at a time.

A skill you rejected with its file is sent once more without it, so you can index it from its record. A pair you
rejected without a file is not sent again until its hash changes. A sync that fails waits 30 s before the next one,
twice as long after each further failure, up to 10 minutes. A sync that only sends you again records you accepted
before waits the same way, because you took them and did not index them; a changed record, such as a new
description for the same file, is new. A sync whose missing pairs you all rejected before waits too. Each exchange gets `sync_ms`, 60 s by default, apart from
the turn's own time, and a failed sync changes no answer. `GET /relay/v1/guidance/sync` with the application token
shows the counts of the last sync that ended and the wait, with no asset names or paths. A sync whose
`find_missing` failed shows `failed` with zero counts.

Two costs come with this design. `not_ready` answers the whole select, so one pair you have not indexed sends every
turn to word match until a sync gets it indexed, unless you give a partial answer (below). And a sync that hangs holds the one sync slot for up to three
`sync_ms` (3 minutes by default), and every other sync is skipped meanwhile.

A selector that can rank the pairs it has not indexed from the select itself, for example by word match over their
names and descriptions, may answer before it has indexed every offered pair (spec 0.75). Answer `ranked` or
`no_match` over the whole offer and end your `mechanism` with `:partial`, for example `"mechanism":"my_rule:partial"`.
Use the same label for any answer that is not your full ranking for a reason other than indexing under way, such as
your embedding model being down while you answer by word match. The gateway passes the answer on with
`degraded: false`, adds no word match of its own, and still syncs after it, as after `not_ready`; when nothing is
missing, that sync is one `find_missing`. If you cannot rank the part you have not indexed, answer `not_ready`.
Comparison runs count partial answers in their own column, because they are not your full ranking.

`scripts/guidance_selector_server.py --sync-root DIR` is a reference selector for this protocol: it serves `/sync`
and `/feedback`, reads files only under its roots and answers `not_ready` until it has every offered pair, or a
`:partial` answer with `--partial`. It still ranks by description, so it shows the protocol, not a better ranking.

### What each identifier means

- `content_hash` names the source: the SHA-256 of the whole `SKILL.md` (up to 1 MiB) when the plugin sent it,
  otherwise the description as sent. No request, task, session or catalog ID goes into it, so an unchanged skill
  keeps the same `{asset_id, content_hash}` in every request and every offer.
- Your own feature recipe versions and input fingerprints decide when you may reuse what you derived from a
  source. Keep them inside your selector; they are not part of this protocol.
- `catalog_revision` names one offer: the exact list of one select.
- `index_revision` in your answer names the retrieval generation you scored with.
- "Searchable" and "every optional feature done" are different states. While any offered pair is unknown to you,
  answer `not_ready` or a `:partial` answer: the gateway syncs only after those, so an unlabeled ranking of a subset
  would leave the unknown pair without a sync. A pair you know and are still indexing may be ranked from the
  features you have, with a `mechanism` and `score_kind` ending in `:early` (spec 0.73); the gateway starts no
  sync for it. Keeping several versions of one asset is planned, not part of the contract yet.
- The gateway does not cache your rankings. It asks you on every request, so identical requests may get a new
  answer after you publish new features.
- Rank with the description of the current select. The same file hash can come with a description shortened or
  redacted differently than before. asset-router `b54416b` and later answer `not_ready` to such an offer and
  report the pair in `find_missing`, so the gateway uploads the current record, which counts as progress. The
  gateway remembers only the record you last accepted for each asset, so switching back to an earlier version or
  description is progress too. A selector that keeps one version of each asset re-indexes on each such change.
- A tool record's `input_schema` is the placeholder `{"type":"object"}` and its `schema_hash` is fixed, so it
  does not tell you when a real tool's schema changed. Real MCP schemas need a separate contract extension.

### Tell your selector what a turn used

A selector learns a user's own skills best from that user's real requests, and most from the turns where it missed
one, which only the host sees (spec 0.75, RM48). Add `feedback_url = "http://127.0.0.1:8940/feedback"` next to
`sync_url`; feedback needs sync. Each answer whose select your selector answered validly (`ranked`, `no_match` or
`not_ready`) then carries `recommendation_id`, the ID of that select. When the turn ends with success, the plugin
reports which offered assets it used, and after answering the plugin the gateway sends one POST to your
`feedback_url`:

```json
{"schema_version":1,"operation":"feedback","feeder_id":"guidance",
 "recommendation_id":"7e46678a-d704-4027-b916-2c84de864101",
 "used":[{"asset_id":"host:skill:pdf-merge","content_hash":"<as offered>","kind":"skill","signal":"loaded"},
         {"asset_id":"host:mcp_tool:weather","content_hash":"<as offered>","kind":"mcp_tool","signal":"executed"}],
 "not_offered":0}
```

Answer `{"schema_version":1,"operation":"feedback","inference":"local","missing":null,"results":null}`.

- `used` holds every offered asset the turn used, whether you picked it or not. An empty list is a signal too.
  `not_offered` counts the assets the turn used that this select did not offer, as above 1,000 host assets, so it
  tells you whether the offer limit, not your ranking, lost an asset.
- `loaded` means `skills_read` of the skill, or a plain `read` inside its folder, returned without error;
  `executed` means a call of the tool returned without error. Under `limit = "strict"`, `refused` means the limit
  turned down a call of an offered asset you did not pick: the clearest miss. A later use in the turn replaces it.
- Not seen: whether the model followed the skill, a subagent's own run, and a skill the user starts with OpenClaw's
  own command, which OpenClaw does not show to a plugin's hooks. A turn that fails sends nothing. A turn that builds
  its prompt again, as on a retry, keeps what it used so far, and the latest select gets the feedback.
- No prompt text, path or body is sent. You already have the request under its `recommendation_id`; keep what you
  choose, on this machine.
- Feedback goes only to a selector whose last valid answer said `"inference":"local"`, unless the operator sets
  `sync_allow_remote = true`. Without that setting, if no sync has told the gateway yet, it first sends a
  `find_missing` with that select's pairs to `sync_url` only to learn this, and uploads nothing. That question
  takes the one sync slot and keeps the sync's wait: while a sync runs, or after a failed sync, the feedback is
  skipped, and a failed question starts the wait like a failed sync. If your selector said `remote`, the gateway
  asks again once 10 minutes have passed since a sync, an earlier question or a feedback answer last told it, so a
  selector restarted with local inference gets feedback again.
- Each select gets at most one feedback. The gateway remembers a select for at most one hour, at most 1,024 of
  them, also above 1,000 host assets, where every select is its own offer, and sends one feedback at a time; a feedback that finds 16 others waiting is dropped. Each exchange gets
  `sync_ms`, there is no retry, and a failure changes no answer.
- `GET /relay/v1/guidance/sync` adds `feedback_enabled`, `feedback_sent`, `feedback_failed` and `feedback_skipped`
  (remote inference, a full queue, an inference question that could not be sent, or a select the gateway no
  longer remembers), counted since the gateway started.

### Check against the real asset-router

`python scripts/asset_router_interop.py --rev <router commit>` clones asset-router at that exact commit into
`~/.cache/relaymind-interop`, runs `npm ci`, and runs `tests/test_guidance_router_interop.py` against the
router's own server, with a deterministic embeddings double instead of a model. It covers a cold router through
`not_ready`, sync and the router's own ranking, a mixed upload, a refused file sent again without content, an
indexing failure retried, reuse across offers, a new feature recipe, a restart and a new description for the same
file. It checks that two programs
work together, not how well a real model ranks.

## Add MCP tools

OpenClaw's Tool Search already keeps MCP tool schemas out of the request by default, so tools cost little
context. A plugin can refuse a tool call, but OpenClaw's plugin API gives it no list of the host's tools, so
guidance gets tools from the plugin's `toolsFile`, a JSON list of `{"name","description"}`.

`relaymind openclaw guidance --tools-file PATH` writes that file from `openclaw mcp probe --json`, which connects to
each server in `mcp.servers` and applies its tool filter. Run it again when your servers change. If a server does not
answer, nothing is changed, because a short list would leave the missing tools outside a strict limit.

OpenClaw's list has names only, such as `weather__get_weather`. Such a tool matches on the words of its name. Add a
`description` in the file for better matches: a new run keeps it. Tools from elsewhere, such as mcporter, are not in
`mcp.servers`; add them by hand, without the `generated` field the command writes, and a new run keeps them too.

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

If your selector builds an index and answers `not_ready` to assets it has not indexed, add
`--sync-url http://127.0.0.1:8940/sync`. The harness then syncs it before any case runs, the same way the gateway
does, and measures only when the selector answers with its full ranking. If that takes longer than
`--sync-wait-s` (600 s by default), it stops and measures nothing. These assets are JSON with no `SKILL.md`, so each
upload carries only the record (`content: null`). The record's name and description are the whole asset, so the
selector indexes it from that. The report's `sync` field shows how long the sync took. That time is not counted in
any case's latency.

Judge a selector by **macro precision and macro recall** first (spec 0.74). They are measured over the prompts
that have an answer to find: one-skill prompts and big workflows. Every pick counts, and a prompt with no pick
scores 0, so a selector cannot look precise by staying silent. The tool also prints them per prompt language, under
"By language".

| Main metric | Meaning | Better |
| --- | --- | --- |
| Macro P | Mean over prompts of the share of picks that are right; 0 for a prompt with no pick | Higher |
| Macro R | Mean over prompts of how much was found: 1 when a one-skill prompt got any acceptable asset; for a big workflow, the share of its needed set | Higher |
| Micro P, Micro R | The same over all picks and all needed assets together | Higher |
| P wins/losses, R wins/losses (p) | Prompts where only one side had better precision, or recall, with a sign test | More wins |

Lowering a threshold usually raises recall and lowers precision. Read both together, never one alone. The other
metrics explain the result:

| Secondary metric | Meaning | Better |
| --- | --- | --- |
| First pick | The first pick is right | Higher |
| MRR | Mean of 1 / the rank of the first right pick, 0 when none | Higher |
| P@3 | Of the first 3 picks, the share that are right. Prompts with no pick are left out, so a selector that often answers `no_match` looks better here than it is | Higher |
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
| First pick wins/losses (p) | Prompts where only one side had a right first pick, or more of a workflow's set, with a sign test; repeated runs of a prompt count once | More wins |

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

Its third-party arms set `fallback = false`. So, against a selector that builds an index, give
`--third-party-sync-url` (your selector's `/sync`), and `--third-party-name` and `--third-party-version` with the
name and version it reports. Before the measured turns, the harness runs warm-up turns that it answers itself, so
they cost nothing. In each one, the real plugin offers the host's skills with their files, and the gateway syncs the
selector. The measured turns start once the selector answers with its full ranking. If that takes longer than
`--sync-wait-s` (600 s by default), the arm stops with no turn measured. `sync.json` in the output folder records the
warm-up, the gateway's sync counts, and how the selector answered the measured turns.

Results so far are in [EVIDENCE.md](EVIDENCE.md#2026-11-05--per-prompt-guidance-development-runs-284). They are
development diagnostics: the expected answers are developer-authored, and they do not satisfy AC25 to AC37.

## Compare your selector with the default, step by step

This runs both benchmarks for any third-party selector against the built-in default (spec 0.74 section 7.7;
RM47). Selection checks the picks against the expected skills. Real turns check whether the host's model then loads
the right skill, and at what input size. asset-router is the worked example at the end; nothing in steps 1 to 5 is
specific to it.

**1. What your selector needs.**

- `POST /select` that speaks selection v2, as in [Connect your own algorithm](#connect-your-own-algorithm). It
  reports a `name` and a `version`. Pass the same values to the harness, because the gateway checks them.
- `POST /sync`, only if your selector builds an index and answers `not_ready` for assets it has not indexed yet. See
  [Keep your selector in sync](#keep-your-selector-in-sync). A selector with no index needs no sync.
- It listens on loopback. If it calls a model, such as an embedding model, run that model on loopback too. Then
  the selector can report `inference: local` and no prompt leaves the machine.

**2. Start two instances.** One is for selection, the other for real turns. Each has its own fresh store or
index, because the two runs offer different assets. The real-turn instance reads skill files only from the folder
where the turn harness installs them, `<turn output>/workspace/skills`. If your selector drops a root that does not
exist, create that folder before you start it.

**3. Selection.** Run the default, your selector, and, as a reference, a model picker over the same prompts:

```bash
python scripts/guidance_similar_benchmark.py --split holdout --arm word --output word.json
python scripts/guidance_similar_benchmark.py --split holdout --arm third_party \
  --selector-url http://127.0.0.1:<port>/select --sync-url http://127.0.0.1:<port>/sync \
  --selector-name <name> --selector-version <version> --output mine.json
RELAYMIND_EXTERNAL_API_KEY=... python scripts/guidance_similar_benchmark.py --split holdout \
  --arm external_picker --model-base-url https://host/v1 --model some-model \
  --request-option reasoning_effort=low --output picker.json
python scripts/guidance_compare.py --baseline word=word.json --arm mine=mine.json \
  --arm picker=picker.json --by-lane --output compare.json
```

Leave out `--sync-url` for a selector with no index. Without a background, only the 70 holdout skills are offered.
To offer the catalog's skills and tools too, as the recorded runs do, build the source catalog
([SOURCE_BENCHMARK.md](SOURCE_BENCHMARK.md)) and add `--background-catalog <catalog folder>` to every arm. Before
you read the numbers, check that the report's `sync.outcome` is `ready`, that no row has `degraded: true`, and
that Failed and Early are 0. Tune on `--split development`, and run the holdout once for the report.

A slow selector, such as one with a large embedding model on a CPU, can miss the gateway's time limit. A row then
falls back to word match and shows `degraded: true`. Give such a selector more time with `--total-ms` (for example
60000), and a long first indexing more with `--sync-wait-s`. Record both, and run again any report that has a
`degraded` row. Keep the selector and its model server running until the whole run ends.

**4. Real turns.** The default arm runs twice, because identical runs differ:

```bash
RELAYMIND_EXTERNAL_API_KEY=... python scripts/guidance_turn_benchmark.py --similar holdout --per-lane 20 \
  --arms default,default_repeat,third_party --model-base-url https://host/v1 --model some-model \
  --third-party-url http://127.0.0.1:<turn port>/select \
  --third-party-sync-url http://127.0.0.1:<turn port>/sync \
  --third-party-name <name> --third-party-version <version> --output /abs/turns
```

Run a short pilot first with `--per-lane 2`, and read the token counts in its `summary.json`. The output folder holds
`summary.json` (right skill, input sizes and time per arm), `runs.jsonl` (one line per turn) and `sync.json` (the
warm-up and how the selector answered the measured turns). With `--similar`, the host offers the 70 holdout skills
and its own bundled skills; the catalog is not installed. To add a selector to runs you already have, run only its
arm (`--arms third_party`) with the same `--similar`, `--per-lane`, `--seed` and model. Its rows then pair with the
earlier ones prompt by prompt.

Before the measured turns, the harness checks two prompts and stops if neither got a hint ("no prompt got a hint").
A selector that answers `no_match` almost everywhere at its threshold stops there, and so does one that is down.
The gateway's time limit in real turns is fixed at 12 s.

**5. Read the results.**

- Lead with **Macro P and Macro R**, overall and under "By language". P wins/losses and R wins/losses show whether
  a difference is more than noise. A change that raises one and lowers the other is a trade-off, not a gain.
- Read false picks on unrelated prompts beside them. A selector that picks something for every prompt gets high
  recall and fails there.
- In real turns, compare the right-skill count per language with both default runs. The two default runs already
  differ on several prompts. Count the turns that got `no_match` (`measured_selects` in `sync.json`, one select per turn after the warm-up, the
  check prompts included, since they are measured turns too): the guided arms
  hide the skill list, so such a turn has no hint and no list.
- Selection latency on one machine that also runs the selector's model is not a speed result. These data and
  expected answers were written by a model, so the results are development diagnostics and do not satisfy AC25 to
  AC37.

### Example: asset-router with local embeddings

asset-router does not embed text itself. It calls an OpenAI-compatible `{embeddings.baseUrl}/v1/embeddings` and
compares the prompt with each asset's description. Serve the embedding model with llama.cpp's server on loopback,
so the router reports `inference: local` and may read skill files:

```bash
# llama.cpp b12237, llama-b12237-bin-ubuntu-x64.tar.gz,
#   sha256 ffd7ccfe4ca5f176f730988e049630ab0b943c5f1a1b005f1619a7048a745f68
# ggml-org/bge-m3-Q8_0-GGUF at 9eba04c5d75ba5a1595e45de734d36bef4e5cb98, bge-m3-q8_0.gguf,
#   sha256 aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173
llama-server -m bge-m3-q8_0.gguf --embeddings --pooling cls --host 127.0.0.1 --port 8950 \
  -c 8192 -b 8192 -ub 8192 -t 6
```

Use an asset-router checkout with `npm ci` done, such as the one `scripts/asset_router_interop.py` keeps in
`~/.cache/relaymind-interop`. Each instance runs from its own working folder and reads `config/local.json` from it:

```json
{
  "skills": {"source": "relaymind"},
  "store": {"filename": "/abs/router-select/store/index.db"},
  "embeddings": {"baseUrl": "http://127.0.0.1:8950", "model": "bge-m3", "dimensions": 1024},
  "server": {"host": "127.0.0.1", "port": 8940, "name": "asset-router", "version": "1"},
  "sync": {"allowedRoots": []}
}
```

```bash
cd /abs/router-select && node --import <checkout>/node_modules/tsx/dist/loader.mjs \
  <checkout>/src/apps/server/main.ts
```

The selection instance needs no roots. For real turns, use port 8941, another store, and
`"allowedRoots": ["/abs/turns/workspace/skills"]`.

**Other embedding models.** Any model llama.cpp can serve as embeddings works. Set `embeddings.model`, set
`embeddings.dimensions` to the model's vector size, and use a fresh store, because vectors of two models cannot be
mixed. These have been measured (results in [EVIDENCE.md](EVIDENCE.md#2026-11-24--asset-router-with-four-embedding-models-rm47-ac42)):

| Model | GGUF | `--pooling` | `dimensions` | Notes |
| --- | --- | --- | ---: | --- |
| `bge-m3` | `ggml-org/bge-m3-Q8_0-GGUF`, above | `cls` | 1024 | asset-router's `minScore` default was set on this model |
| `ibm-granite/granite-embedding-97m-multilingual-r2` at `835ad14` | none published; convert to F16 | `cls` | 384 | 97M parameters |
| `nomic-ai/nomic-embed-text-v2-moe-GGUF` at `ffbcf4c`, `nomic-embed-text-v2-moe.Q8_0.gguf` (sha256 `06e7a7e5…3cff`) | published | `mean` | 768 | at most 512 tokens per text: serve it with `-c 2048 -np 4 -b 512 -ub 512`; trained with `search_query: ` and `search_document: ` prefixes, which asset-router does not send |
| `ThakiCloud/SKILLRET-Embedding-0.6B` at `67f1594` | none published; convert to Q8_0 | `last` | 1024 | fine-tuned for skill retrieval, in English; trained with an `Instruct: … Query:` prompt on queries, which asset-router does not send |

To convert a model that has no GGUF, download it at a pinned revision, check its `model.safetensors` against the
SHA-256 that Hugging Face lists, and run the converter of the same llama.cpp release:

```bash
python <llama.cpp b12237 source>/convert_hf_to_gguf.py <model folder> --outtype q8_0 \
  --outfile model-q8_0.gguf
```

Then check that the server's vectors match the original model. Embed a few English and Chinese texts with
`sentence-transformers` and through `/v1/embeddings`: the cosine of each pair should be at least 0.99. A wrong
`--pooling` gives vectors that still look plausible, so this check is not optional.

**Set `minScore` for each model.** asset-router keeps a candidate only above `minScore` (0.55 by default), and that
value was set on `bge-m3`. Models score on very different scales. On the holdout, the right skill's median cosine
with an English prompt was 0.60 with `bge-m3`, 0.87 with granite, 0.54 with nomic and 0.42 with SKILLRET.
With the default threshold, granite therefore keeps almost everything, and nomic and SKILLRET almost nothing. For another model,
first see its scale and how well it ranks by embedding alone, with no selector involved:

```bash
python scripts/guidance_embedding_probe.py --url http://127.0.0.1:8950 --split development \
  --output probe.json
```

It reports, for English, mixed and pure Chinese prompts, how often the right skill ranks first or in the top 3
among the split's skills, and its cosine (minimum, median, maximum). `--query-prefix` and `--document-prefix` show
what a model's prompts would add. Then set the threshold in the router's config, tune it on `--split development`
by macro P and macro R, and run the holdout once:

```json
"router": {"selected": "router.hybrid-bm25", "routers": {"router.hybrid-bm25": {"minScore": 0.40}}}
```

asset-router's BM25 stage keeps only `[a-z0-9]` in each token, and only BM25 hits become candidates unless
`recallUnmatched` is true. So a prompt with no Latin word gets `no_match` whatever the embedding model.
