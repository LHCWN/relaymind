# 按提示词引导

[English](guidance.md)

用户在 OpenClaw 中输入提示词后，RelayMind 只提议该提示词需要的少量技能和工具，让模型看到这几项，而不是一长串列表。如果你愿意，也可以把 OpenClaw 限制在这几项之内。需求 RM45 和 RM46，规范第 7.7 节。

网关只做提议。它不会运行任何东西，也不会授予权限。如果它很慢或失败，本轮对话会在没有提示、也没有限制的情况下继续，OpenClaw 自己的搜索仍然可用。

## 先了解这几点

- **无需等待。** 回答不需要索引，也不需要模型。它读取主机随每条提示词发送的列表，所以刚安装的技能在下一条提示词就会生效。
- **混合语言。** 单纯的词匹配无法把中文提示词和英文技能对应起来。小型本地模型会写出英文关键词，能解决其中很大一部分问题，而且不需要 API 密钥。
- **选对一组，而不是一个。** 简单的提示词得到一个技能。大型工作流的每个任务各得到一个，通常是 3 到 5 个，有时更多。默认上限是 8，`max_picks` 最高可设为 20。
- **相似的技能。** 技能市场上常有许多功能几乎相同的技能。要选对，需要一个模型来阅读它们之间的差别，所以 `picker = "external"` 在这里最重要。当提示词同时符合多个技能、又没有说明要哪一个时，提示会让模型询问用户。
- **隐藏，或者限制。** 默认只是隐藏其余技能：模型仍可通过 OpenClaw 的搜索和读取工具加载任何技能，所以选错不会造成严重后果。使用[严格限制](#把-openclaw-限制在所选范围内)时，它还会拒绝这些技能。

## 设置

1. 在网关启动文件中打开它：

   ```toml
   [guidance]
   enabled = true
   keywords = "local"   # none, local (needs [local_model]) or external (needs [external_model])
   picker = "none"      # none, or external: a cheap model chooses the final few
   ```
   | 设置 | 需要 | 最终选择方式 |
   | --- | --- | --- |
   | `keywords = "none"`, `picker = "none"` | 无额外要求 | 最多 8 个词匹配较强的项，由 OpenClaw 的模型选择 |
   | `keywords = "local"`, `picker = "none"` | 一个本地模型 | 同上，并且支持跨语言 |
   | `keywords = "local"`, `picker = "external"` | 一个本地模型和一个 API 密钥 | 由便宜的模型选出提示词需要的那一组 |
   | `keywords = "external"`, `picker = "external"` | 一个 API 密钥 | 同上，效果最好，稍慢一些 |
   | `primary = "http"` | 你的选择器 | 由你的选择器选择 |

   网关启动时需要一个 `[local_model]` 或 `[external_model]` 表，即使引导功能从不调用它。

   只有提示词中含有拉丁字母以外的文字时才会运行关键词步骤，所以英文提示词依然很快。当选择模型什么也没找到时，任何提示词都会再运行一次关键词步骤，因为提示词可能和正确的技能没有共同的词。像“好的，就这么做”这样简短、自身的词又找不到强匹配的后续提示词，会和用户刚才说的话一起匹配。

2. 在代码仓库根目录启动网关：`relaymind serve --config relaymind.toml`。它会打印一个应用令牌。把它放到 OpenClaw 进程的 `RELAYMIND_APP_TOKEN` 中。

3. 用一条命令设置 OpenClaw。它运行 OpenClaw 自己的命令来安装[引导插件](../integrations/openclaw/guidance-plugin/README.md)、允许它的钩子并设置网关地址。确认插件已加载后，它才把 `skills.limits.maxSkillsInPrompt` 设为 `0`：

   ```bash
   relaymind openclaw guidance --tools-file ~/.relaymind/tools.json
   ```

   只提供技能时可省略 `--tools-file`。需要[严格限制](#把-openclaw-限制在所选范围内)时加上 `--limit strict`。省略的选项会保留之前运行时的设置；要停止提供工具，请运行 `openclaw config unset plugins.entries.relaymind-guidance.config.toolsFile`。

   `maxSkillsInPrompt` 对该 OpenClaw 的所有智能体生效。插件只声明支持 OpenClaw 2026.10.7，该命令会拒绝其他版本。升级 OpenClaw 后主机可能跳过插件，这时请运行 `openclaw config unset skills.limits.maxSkillsInPrompt` 恢复技能列表。

   技能很多时，还要调高 `skills.limits.maxSkillsLoadedPerSource`（默认 200）和 `maxCandidatesPerRoot`（默认 300）。超过这些上限的技能 OpenClaw 不会加载，因此既不能被选择，也无法通过搜索找到。当会话达到第一个上限时，插件会记录一条警告。

4. 检查。在 OpenClaw 中提问，然后在请求的提示词开头查找 `[RelayMind guidance, not from the user]`。也可以直接调用网关：

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

   `degraded: true` 表示某个已配置的步骤失败了，`stages` 会说明是哪一个。结果看起来不好时请检查这里：关键词模型的端口配置错误时，系统会悄悄退回到普通词匹配。

## 把 OpenClaw 限制在所选范围内

在[插件配置](../integrations/openclaw/guidance-plugin/README.md)中设置 `limit`：

```json
{"baseUrl": "http://127.0.0.1:8080/v1", "limit": "strict"}
```

之后，对每条提示词，插件都会拒绝下列调用，并在消息中列出已选择的项：

- 对未被选择的技能调用 `skills_read`；
- `skills_search`，因为它会把目录的其余部分带入上下文；
- 对未被选择技能的文件夹内容使用普通 `read`，无论路径如何书写；
- 调用 `toolsFile` 中未被选择的工具。

本会话已经成功用过的技能或工具仍然可用，所以后续提示词不受影响。提议失败、没有选择任何项、不是用户发起的轮次，或者其他会话（例如子代理的会话），都不设限制。失败绝不会阻塞对话。

选择它之前要知道两点：

- 选错现在会失去正确的技能，因为搜索无法补救。请和选择模型一起使用。
- 它限制的是普通使用，并不是安全隔离：`exec` 或任何能访问文件系统的工具仍可读取技能，`toolsFile` 之外的工具也不受限制；在 OpenClaw 的代码模式中，`skills.list()` 不经过工具调用就能列出所有技能，任何钩子都看不到。如果需要隔离，还要用 OpenClaw 自己的工具策略移除这些工具，并关闭代码模式。

## 接入你自己的算法

选择这一部分本来就是可以替换的。你的算法取代词匹配、关键词和选择模型，之后的一切对任何选择器都相同：提示、严格限制和指标。

你的算法是一个响应单个请求的本地 HTTP 服务。协议已公开：[请求](../src/relaymind/selection/schemas/selection_request.v2.json)、[响应](../src/relaymind/selection/schemas/selection_result.v2.json)。[`scripts/example_intent_selector.py`](../scripts/example_intent_selector.py) 是一个约 200 行的完整选择器，不依赖 RelayMind。复制它即可开始。

```bash
python scripts/example_intent_selector.py --port 8940
```

不接模型时它只做词匹配，因此中文提示无法匹配英文技能。请加上 `--model-url`、`--model`、`--api-key-env`；如果模型默认会先思考，再加上 `--reasoning-effort low`。

```toml
[guidance]
enabled = true
primary = "http"                         # required: without it the URL is ignored
name = "example_intent"                  # must equal the name your result returns
version = "1"                            # raise it when behavior changes
url = "http://127.0.0.1:8940/select"
total_ms = 12000
```

同一个选择器也可用于[推荐](asset-selection.md#connect-a-team-selector)：配置表是 `[recommendations]`，并且必须把 `primary_ms` 和 `total_ms` 从默认的 150 ms 和 500 ms 调大。

请求如下（已缩短）：

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

你的响应必须原样返回这些 ID，并且只能提议所提供的记录：

```json
{"schema_version":2,"recommendation_id":"7e46678a-d704-4027-b916-2c84de864101","catalog_revision":1,
 "host_snapshot_revision":null,"selector":{"name":"example_intent","version":"1"},"mechanism":"my_rule",
 "outcome":"ranked","decision":"choice_required","index_revision":null,"elapsed_ms":3,"usage_claims":null,
 "candidates":[{"asset_id":"host:skill:pdf-merge","kind":"skill","content_hash":"<same as offered>",
   "source_version":null,"score":1.0,"score_kind":"my_score","evidence_refs":["/description"]}]}
```

规则：

- 只接受环回地址上的普通 `http`，每次尝试一个 POST。没有重定向、重试或身份验证。
- 用 HTTP 200 和 `application/json` 响应，最大 1 MiB。不能有重复的 JSON 键或非有限数字。
- `mechanism` 是你自己的标签。不要使用 `lexical_overlap`、`gateway_lexical_fallback` 或内置选择器的名称，它们会被拒绝。
- 没有合适项时，响应 `"outcome":"no_match","decision":"no_match","candidates":[]`。
- 失败或无效的响应会退回到词匹配并标记 `degraded: true`，除非设置 `fallback = false`。
- 选择器只做提议。网关只接受所提供的准确记录，提议本身不会运行任何东西。

主机资产超过 1,000 项时，你的选择器会收到词匹配最好的 1,000 项；内置步骤最多处理 20,000 项。托管服务前面需要一个小型本地进程，因为 URL 必须在环回地址上。

## 在第一个提示词之前做准备

插件随每个提示词发送主机的列表，所以没有索引的选择器无需任何准备。需要建立索引的选择器不必等到第一个提示词：OpenClaw 无需会话就能用它自己的发现、优先级和上限规则列出同样的技能，所以不要自己遍历技能目录。

```bash
openclaw skills list --eligible --json    # 保留 modelVisible 为 true 的技能；用 --agent <id> 指定智能体
openclaw skills info <name> --json        # filePath 是该技能的 SKILL.md，包含完整正文
```

请求中不包含技能正文，请从 `filePath` 读取。`source_locator` 只是标签 `host://skill/<name>`，不是路径。`skills list` 不输出路径，所以每个技能都要调用一次 `skills info`，每次需要几秒钟，请在后台运行。

这只是预热。能选哪些由每个请求中的列表决定，它可能因智能体和会话而不同。用技能名 `identity.package_id` 匹配已索引的记录。`content_hash` 只覆盖发送的描述（经过截断和脱敏），技能正文变化时哈希可能不变，所以还要检查文件。

对于 MCP 工具，请求中的 `input_schema` 是占位符 `{"type":"object"}`，因为 OpenClaw 的 `mcp probe` 只列出名称。需要真实 schema 的选择器要自己用 `tools/list` 询问服务器；`openclaw mcp show` 会显示如何连接它们。

## 添加 MCP 工具

OpenClaw 的 Tool Search 默认已把 MCP 工具的 schema 排除在请求之外，所以工具占用的上下文很少。插件可以拒绝工具调用，但 OpenClaw 的插件 API 不向它提供主机的工具列表，所以引导功能从插件的 `toolsFile` 获取工具，它是一个 `{"name","description"}` 的 JSON 列表。

`relaymind openclaw guidance --tools-file PATH` 根据 `openclaw mcp probe --json` 写入这个文件，该命令会连接 `mcp.servers` 中的每个服务器并应用其工具过滤器。服务器变化后请重新运行。如果有服务器没有响应，则不做任何更改，因为列表不完整会让缺失的工具不受严格限制。

OpenClaw 的列表只有名称，例如 `weather__get_weather`，这样的工具只按名称中的词匹配。在文件中添加 `description` 可以提高匹配效果，重新运行时会保留。来自其他地方（例如 mcporter）的工具不在 `mcp.servers` 中，需要手动添加，不要带命令写入的 `generated` 字段，这样重新运行时也会保留。

推荐目录中的工具通过元数据导入进入。`content_hash` 是 `original_path` 处文件的 SHA-256，`schema_hash` 是 `input_schema` 的规范摘要（[schema](../src/relaymind/assets/schemas/asset_metadata_import.v1.json)）：

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

## 与基线比较

最快的比较方式除了你的选择器之外不需要任何准备。它使用 [`similar.v1.json`](../evaluation/assets/guidance-similar/similar.v1.json) 中的技能：一些功能几乎相同、只在一个实际方面不同的技能族，并带有英文和中文提示词、后续提示词、符合整个技能族的提示词，需要多个技能的复杂请求，以及无关的提示词。

```bash
python scripts/guidance_similar_benchmark.py --split holdout --arm word --output word.json
python scripts/guidance_similar_benchmark.py --split holdout --arm third_party \
  --selector-url http://127.0.0.1:8940/select --selector-name example_intent --output mine.json
python scripts/guidance_compare.py --baseline word=word.json --arm mine=mine.json --by-lane
```

| 指标 | 含义 | 更好的方向 |
| --- | --- | --- |
| First pick | 第一个选择正确 | 越高越好 |
| MRR | 第一个正确选择排名的倒数的平均值，没有时为 0 | 越高越好 |
| P@3 | 前 3 个选择中正确的比例。没有任何选择的提示词不计入 | 越高越好 |
| R@3, R@8 | 前 3 或前 8 个选择中包含正确答案 | 越高越好 |
| F1@3 | 2 x P@3 x R@3 / (P@3 + R@3) | 越高越好 |
| Picks | 每条提示词实际给出的选择数 | 越低越好 |
| False picks | 无关的提示词得到了选择 | 越低越好 |
| Asked | 符合多个资产的提示词得到 `decision: choose` | 越高越好 |
| Offered | 这些资产中被提供的比例 | 越高越好 |
| Set found | 大型工作流所需的那组资产中被选中的比例 | 越高越好 |
| All found | 大型工作流所需的整组资产都被选中 | 越高越好 |
| Set exact | 大型工作流的选择中确实需要的比例 | 越高越好 |
| Set picks | 大型工作流每条提示词的选择数 | 接近所需数量 |
| Failed | 某个步骤或选择器失败 | 越低越好 |
| Wins/losses (p) | 只有一方第一个选择正确的提示词数，并附符号检验 | 胜场越多越好 |

选择协议 v2 没有 `choose`，所以第三方选择器的 Asked 显示为 0%，平台基准测试的报告显示为 "-"。Failed 统计的是选择器自己没有作答的情况，即使后备方案作答了也算。同一个提示词重复运行多次，在符号检验中只算一次。请在 `--split development` 上开发，在 `--split holdout` 上报告结果，不要针对 holdout 调参。无关提示词和简短的追问语句在两个划分中相同，所以这两条通道不是留出数据。

同一个工具也能读取更大的源基准的报告。在相同提示词上、通过相同的网关路径进行比较。按 [SOURCE_BENCHMARK.md](SOURCE_BENCHMARK.md) 构建一次数据。然后对每个实验组运行一个选择器，并执行：

```bash
python scripts/guidance_selector_server.py --port 8941 --name guidance_local --keywords local &
python scripts/source_asset_benchmark.py run --catalog catalog-v3/catalog.json \
  --cases evaluation/assets/matrix-development/cases.v3.json --evidence-root catalog-v3 \
  --production my-arm-production.toml --track service --output my-arm.json
python scripts/source_asset_benchmark.py compare --left builtin.json --right my-arm.json --output cmp.json
python scripts/guidance_compare.py --baseline builtin=builtin.json --arm mine=my-arm.json
```

两个实验组只有在时间限制和回退设置完全相同时才能比较。在两个生产配置文件中把 `total_ms`、`primary_ms` 和 `fallback` 设为相同的值，否则 `compare` 会返回 `incompatible_development_runs`。在报告中，`mechanism` 以 `_degraded` 结尾的实验组有步骤失败：不要把它当作你配置的那个实验组来解读。

模型之后是否加载了正确的技能，是另一个问题。`scripts/guidance_turn_benchmark.py` 会通过 OpenClaw、插件、网关和外部模型运行真实的完整对话轮次；用 `--similar holdout` 测试相似技能，用 `--tools` 测试 MCP 工具。请把默认实验组运行两次：相同的运行结果也会有差异，任何改进都必须超过这种噪声。

目前的结果见 [EVIDENCE.md](EVIDENCE.md#2026-11-05--per-prompt-guidance-development-runs-284)。它们只是开发诊断：期望答案由开发者编写，不满足 AC25 到 AC37。
