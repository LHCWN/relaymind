# 按提示词引导

[English](guidance.md)

用户在 OpenClaw 中输入提示词后，RelayMind 只提议该提示词需要的少量技能和工具，让模型看到这几项，而不是一长串列表。如果你愿意，也可以把 OpenClaw 限制在这几项之内。需求 RM45 至 RM47，规范第 7.7 节。

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
   "content_hash":"<sha256 of SKILL.md>","description":"Merge pdf documents","language_tags":[],
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
- 失败、无效的响应或 `"outcome":"not_ready"` 会退回到词匹配并标记 `degraded: true`，除非设置 `fallback = false`。`mechanism` 以 `:partial` 结尾的部分响应不算失败；见[让选择器保持同步](#让选择器保持同步)。
- 选择器只做提议。网关只接受所提供的准确记录，提议本身不会运行任何东西。

主机资产超过 1,000 项时，你的选择器会收到词匹配最好的 1,000 项；内置步骤最多处理 20,000 项。托管服务前面需要一个小型本地进程，因为 URL 必须在环回地址上。

## 在第一个提示词之前做准备

插件随每个提示词发送主机的列表，所以没有索引的选择器无需任何准备。需要建立索引的选择器不必等到第一个提示词：OpenClaw 无需会话就能用它自己的发现、优先级和上限规则列出同样的技能，所以不要自己遍历技能目录。

```bash
openclaw skills list --eligible --json    # 保留 modelVisible 为 true 的技能；用 --agent <id> 指定智能体
openclaw skills info <name> --json        # filePath 是该技能的 SKILL.md，包含完整正文
```

请求中不包含技能正文，请从 `filePath` 读取。`source_locator` 只是标签 `host://skill/<name>`，不是路径。`skills list` 不输出路径，所以每个技能都要调用一次 `skills info`，每次需要几秒钟，请在后台运行。

这只是预热。能选哪些由每个请求中的列表决定，它可能因智能体和会话而不同。用技能名 `identity.package_id` 匹配已索引的记录。插件会发送每个技能的 SHA-256，所以技能的 `content_hash` 是其 `SKILL.md` 的哈希，正文变化时它也会变化。文件大于 1 MiB 的技能没有这个哈希，其 `content_hash` 只覆盖发送的描述（经过截断和脱敏）。

对于 MCP 工具，请求中的 `input_schema` 是占位符 `{"type":"object"}`，因为 OpenClaw 的 `mcp probe` 只列出名称。需要真实 schema 的选择器要自己用 `tools/list` 询问服务器；`openclaw mcp show` 会显示如何连接它们。

## 让选择器保持同步

为技能正文建立索引的选择器可以让网关告诉它哪些内容变了（规范 0.72，RM47）。在上面的 `[guidance]` 表中加入
`sync_url = "http://127.0.0.1:8940/sync"`。当你的选择器响应 `"outcome":"not_ready"` 时，网关在这一轮退回到词匹配，并在作答之后向该 URL 发送最多三个 POST：

1. `{"schema_version":1,"operation":"find_missing","feeder_id":"guidance","assets":[{"asset_id","content_hash"}]}`，包含所有提供的记录。用 `{"schema_version":1,"operation":"find_missing","inference":"local","missing":[...],"results":null}` 响应，列出你尚未索引的配对。
2. `{"schema_version":1,"operation":"upload","feeder_id":"guidance","assets":[{"record","content"}]}`，只包含这些配对。`record` 是选择请求中的原样记录。技能的 `content` 是 `{"kind":"file","path":"/abs/SKILL.md","size_bytes":812}`，工具或没有文件的技能为 `null`。用 `"missing":null` 响应，并为每个配对给出一个结果：接收并准备建立索引时为 `{"asset_id","content_hash","status":"accepted","reason":null}`，否则为 `"status":"rejected"`，`"reason"` 取 `hash_mismatch`、`size_mismatch`、`outside_roots`、`too_large`、`unreadable` 或 `invalid_record` 之一。

由你自己读取文件，只有当文件的真实路径（解析所有符号链接之后）位于运维人员允许的目录下时才读取，并用 `size_bytes` 和 `content_hash` 核对大小和 SHA-256。按准确的 `asset_id` 和 `content_hash` 为每个资产建立索引；如果选择请求提供了你尚未索引的配对，就响应 `not_ready`。既没有建立索引、也没有正在建立索引的配对要报告为缺失，所以索引失败的配对会再次缺失。如果你的嵌入模型或标签模型不在本机，请返回 `"inference":"remote"`：此时网关不发送路径，除非运维人员设置 `sync_allow_remote = true`。网关从不读取或发送技能正文，同一时间最多运行一次同步。

带文件时被你拒绝的技能会再发送一次，但不带文件，这样你可以只根据记录为它建立索引。不带文件时被你拒绝的配对在其哈希变化之前不会再次发送。同步失败后，下一次同步要等待 30 秒，此后每连续失败一次等待时间加倍，最多 10 分钟。如果一次同步只是再次发送你以前接收过的相同记录，它也同样等待，因为你接收了它们却没有建立索引；记录有变化（例如同一文件的新描述）则算作新的。如果缺失的配对都是你以前拒绝过的，这次同步也会等待。每次交换使用 `sync_ms`（默认 60 秒），与本轮自身的时间分开；同步失败不会改变任何响应。用应用令牌调用 `GET /relay/v1/guidance/sync` 可以查看上一次已结束的同步的计数和等待时间，其中不包含资产名称或路径。`find_missing` 失败的同步显示为 `failed`，计数为零。

这种设计有两个代价。`not_ready` 针对整个选择请求，所以只要有一个配对尚未索引，每一轮都会退回到词匹配，直到同步让它建立索引，除非你给出部分响应（见下文）。另外，挂起的同步最多会占用唯一的同步位置三个 `sync_ms`（默认 3 分钟），在此期间其他同步都会被跳过。

如果选择器能仅凭选择请求本身为尚未索引的配对排序（例如对它们的名称和描述做词匹配），就可以在索引完所有提供的配对之前作答（规范 0.75）。对整个提供响应 `ranked` 或 `no_match`，并让 `mechanism` 以 `:partial` 结尾，例如 `"mechanism":"my_rule:partial"`。网关原样传递该响应，标记 `degraded: false`，不额外加入自己的词匹配，并且和 `not_ready` 之后一样，在作答之后同步。如果你无法为尚未索引的部分排序，就响应 `not_ready`。比较运行会把部分响应单独计为一列，因为它们不是你的完整排序。

`scripts/guidance_selector_server.py --sync-root DIR` 是这个协议的参考选择器：它提供 `/sync` 和 `/feedback`，只读取其根目录下的文件，并在拥有所有提供的配对之前响应 `not_ready`，加上 `--partial` 时则给出 `:partial` 响应。它仍然按描述排序，所以它展示的是协议，而不是更好的排序。

### 各个标识的含义

- `content_hash` 标识来源内容：插件发送了文件时是整个 `SKILL.md`（最多 1 MiB）的 SHA-256，否则是所发送描述的哈希。其中不包含请求、任务、会话或目录 ID，所以未改变的技能在每个请求和每次提供中都保持相同的 `{asset_id, content_hash}`。
- 你自己的特征配方版本和输入指纹决定何时可以复用从来源派生的内容。它们留在你的选择器内部，不属于本协议。
- `catalog_revision` 标识一次提供，即一次选择请求的准确列表。
- 你响应中的 `index_revision` 标识你评分时使用的检索代次。
- “可检索”和“所有可选特征都已完成”是不同的状态。只要有提供的配对你还不认识，就响应 `not_ready` 或 `:partial` 响应：网关只在这两种响应之后同步，所以不加标记地只对一部分排序会让未知配对得不到同步。你已认识、仍在建立索引的配对，可以用已有的特征排序，并让 `mechanism` 和 `score_kind` 以 `:early` 结尾（规范 0.73）；网关不会为它启动同步。同时保存一个资产的多个版本是计划中的内容，尚不属于契约。
- 网关不缓存你的排序。它在每个请求中都询问你，所以在你发布新特征之后，相同的请求可能得到新的响应。
- 按当前选择请求中的描述排序。同一个文件哈希可能带着与之前不同的截短或脱敏描述。`b54416b` 及之后版本的 asset-router 对这样的提供响应 `not_ready`，并在 `find_missing` 中报告该配对，于是网关上传当前记录，这算作有进展。网关只记住你为每个资产最后接受的记录，所以切换回较早的版本或描述也算作有进展。只为每个资产保存一个版本的选择器会在每次这样的切换时重新建立索引。
- 工具记录的 `input_schema` 是占位符 `{"type":"object"}`，其 `schema_hash` 固定不变，所以它不能告诉你真实工具的 schema 何时变化。真实的 MCP schema 需要单独的契约扩展。

### 告诉选择器一轮用了什么

选择器最能从用户自己的真实请求中学会该用户的技能，尤其是它漏选了技能的那些轮次，而只有主机能看到这些（规范 0.75，RM48）。在 `sync_url` 旁边加入 `feedback_url = "http://127.0.0.1:8940/feedback"`；反馈需要同步。此后，凡是你的选择器给出有效响应（`ranked`、`no_match` 或 `not_ready`）的选择请求，其响应都会带上该选择请求的 ID `recommendation_id`。这一轮成功结束时，插件报告它用了哪些提供的资产；网关回应插件之后，向你的 `feedback_url` 发送一个 POST：

```json
{"schema_version":1,"operation":"feedback","feeder_id":"guidance",
 "recommendation_id":"7e46678a-d704-4027-b916-2c84de864101",
 "used":[{"asset_id":"host:skill:pdf-merge","content_hash":"<as offered>","kind":"skill","signal":"loaded"},
         {"asset_id":"host:mcp_tool:weather","content_hash":"<as offered>","kind":"mcp_tool","signal":"executed"}]}
```

用 `{"schema_version":1,"operation":"feedback","inference":"local","missing":null,"results":null}` 响应。

- `used` 包含这一轮用过的所有提供的资产，无论你是否选中了它。空列表也是一种信号。
- `loaded` 表示对该技能的 `skills_read`，或在其目录内的普通 `read`，没有出错就返回了；`executed` 表示对该工具的调用没有出错就返回了。模型是否遵循了技能的说明无法观察到，子智能体自己的运行也不计入。失败的一轮不发送任何内容。
- 不发送提示词文本、路径或正文。你已经按 `recommendation_id` 拥有这个请求；保留什么由你决定，且只在本机保存。
- 只有当选择器最近一次有效响应说 `"inference":"local"` 时才发送反馈，除非运维人员设置 `sync_allow_remote = true`。没有这个设置时，如果还没有同步告诉过网关，网关会先向 `sync_url` 发送一个包含该选择请求配对的 `find_missing`，只用来了解这一点，不上传任何内容。
- 每个选择请求最多得到一次反馈。网关最多记住 1,024 个选择请求，每个最多一小时，并且同一时间只发送一个反馈；发现已有 16 个在等待的反馈会被丢弃。每次交换使用 `sync_ms`，不重试，失败不会改变任何响应。
- `GET /relay/v1/guidance/sync` 增加了 `feedback_enabled`、`feedback_sent`、`feedback_failed` 和 `feedback_skipped`（远程推理或队列已满），从网关启动起计数。

### 用真实的 asset-router 检查

`python scripts/asset_router_interop.py --rev <router commit>` 会把该准确提交的 asset-router 克隆到 `~/.cache/relaymind-interop`，运行 `npm ci`，再用确定性的嵌入替身代替模型，针对路由器自己的服务器运行 `tests/test_guidance_router_interop.py`。它覆盖冷启动的路由器经过 `not_ready`、同步到路由器自己的排序，混合上传，被拒绝的文件不带内容重新发送，索引失败后重试，跨不同提供的复用，新的特征配方，重启，以及同一文件的新描述。它检查的是两个程序能否协同工作，而不是真实模型排序的好坏。

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

如果你的选择器会建立索引，并对尚未索引的资产回答 `not_ready`，请加上 `--sync-url http://127.0.0.1:8940/sync`。
这样，测试工具会在运行任何用例之前像网关一样同步选择器，并且只在选择器用完整排序作答后才开始测量。如果超过
`--sync-wait-s`（默认 600 秒），它会停止，不测量任何内容。这些资产来自 JSON，没有 `SKILL.md`，所以每次上传只有记录
（`content: null`）。记录中的名称和描述就是整个资产，选择器据此建立索引。报告中的 `sync` 字段记录同步用了多长时间，
这段时间不计入任何用例的延迟。

评价选择器时，首先看**宏平均精确率和宏平均召回率**（规范 0.74）。它们在有答案可找的提示词上计算：单技能提示词和大型工作流。每个选择都计入，没有任何选择的提示词记为 0，所以选择器不能靠不作答显得精确。工具还会在 "By language" 下按提示词语言分别给出这两个指标。

| 主要指标 | 含义 | 更好的方向 |
| --- | --- | --- |
| Macro P | 各提示词中正确选择所占比例的平均值；没有选择的提示词记为 0 | 越高越好 |
| Macro R | 各提示词找到多少的平均值：单技能提示词选中任一可接受资产记为 1；大型工作流记为所需那组资产中被选中的比例 | 越高越好 |
| Micro P, Micro R | 同样的指标，但把所有选择和所有所需资产合在一起计算 | 越高越好 |
| P wins/losses, R wins/losses (p) | 只有一方精确率或召回率更好的提示词数，并附符号检验 | 胜场越多越好 |

降低阈值通常会提高召回率、降低精确率。请把两者放在一起看，不要只看其中一个。其余指标用来解释结果：

| 次要指标 | 含义 | 更好的方向 |
| --- | --- | --- |
| First pick | 第一个选择正确 | 越高越好 |
| MRR | 第一个正确选择排名的倒数的平均值，没有时为 0 | 越高越好 |
| P@3 | 前 3 个选择中正确的比例。没有任何选择的提示词不计入，所以经常回答 `no_match` 的选择器在这里显得比实际更好 | 越高越好 |
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
| First pick wins/losses (p) | 只有一方第一个选择正确（或找到工作流所需资产更多）的提示词数，并附符号检验 | 胜场越多越好 |

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

它的第三方实验组设置了 `fallback = false`。因此，如果选择器会建立索引，请加上 `--third-party-sync-url`（选择器的
`/sync`），并用 `--third-party-name` 和 `--third-party-version` 给出选择器报告的名称和版本。在测量的轮次之前，
测试工具会先运行几轮预热轮次，由它自己回答，所以不产生费用。每一轮中，真实的插件会提供主机的技能及其文件，网关会同步
选择器。选择器用完整排序作答后，测量的轮次才开始。如果超过 `--sync-wait-s`（默认 600 秒），该实验组会停止，不测量任何
轮次。输出目录中的 `sync.json` 记录预热情况、网关的同步计数，以及选择器对测量轮次的回答。

目前的结果见 [EVIDENCE.md](EVIDENCE.md#2026-11-05--per-prompt-guidance-development-runs-284)。它们只是开发诊断：期望答案由开发者编写，不满足 AC25 到 AC37。

## 逐步将你的选择器与默认设置比较

这里用任意第三方选择器、以内置默认设置为基线，运行两个基准测试（规范 0.74 第 7.7 节；RM47）。选择测试把选出的资产与期望的技能比较；真实轮次检查主机的模型之后是否加载了正确的技能，以及输入有多大。最后以 asset-router 为例；第 1 到 5 步对任何选择器都适用。

**1. 选择器需要什么。**

- 支持选择协议 v2 的 `POST /select`，见[接入你自己的算法](#接入你自己的算法)。它会报告 `name` 和 `version`。把同样的值传给测试工具，因为网关会检查它们。
- `POST /sync`：只有当选择器会建立索引，并对尚未索引的资产回答 `not_ready` 时才需要。见[让选择器保持同步](#让选择器保持同步)。不建立索引的选择器不需要同步。
- 它监听回环地址。如果它会调用模型（例如嵌入模型），也在回环地址上运行这个模型。这样选择器可以报告 `inference: local`，没有提示词离开本机。

**2. 启动两个实例。** 一个用于选择测试，一个用于真实轮次。两个实例各自使用全新的存储或索引，因为两次运行提供的资产不同。用于真实轮次的实例只从测试工具安装技能的目录读取技能文件，即 `<轮次输出目录>/workspace/skills`。如果你的选择器会丢弃不存在的根目录，请在启动它之前创建这个目录。

**3. 选择。** 在相同的提示词上运行默认设置、你的选择器，以及作为参考的模型选择器：

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

不建立索引的选择器去掉 `--sync-url`。没有背景时，只提供 70 个 holdout 技能。如果要像已记录的运行那样同时提供目录中的技能和工具，请构建源目录（[SOURCE_BENCHMARK.md](SOURCE_BENCHMARK.md)），并给每个实验组加上 `--background-catalog <目录文件夹>`。读数字之前，先确认报告的 `sync.outcome` 是 `ready`，没有任何行是 `degraded: true`，并且 Failed 和 Early 都是 0。在 `--split development` 上调参，只为报告运行一次 holdout。

慢的选择器（例如在 CPU 上使用大型嵌入模型的选择器）可能超过网关的时间限制。这时该行会回退到关键词匹配，并显示 `degraded: true`。用 `--total-ms`（例如 60000）给这样的选择器更多时间，用 `--sync-wait-s` 给较长的首次索引更多时间。记录这两个值，并重新运行任何带有 `degraded` 行的报告。在整个运行结束之前，保持选择器及其模型服务器运行。

**4. 真实轮次。** 默认实验组运行两次，因为相同的运行结果也会不同：

```bash
RELAYMIND_EXTERNAL_API_KEY=... python scripts/guidance_turn_benchmark.py --similar holdout --per-lane 20 \
  --arms default,default_repeat,third_party --model-base-url https://host/v1 --model some-model \
  --third-party-url http://127.0.0.1:<turn port>/select \
  --third-party-sync-url http://127.0.0.1:<turn port>/sync \
  --third-party-name <name> --third-party-version <version> --output /abs/turns
```

先用 `--per-lane 2` 做一次简短的试运行，并查看其 `summary.json` 中的 token 数。输出目录包含 `summary.json`（每个实验组的正确技能数、输入大小和时间）、`runs.jsonl`（每轮一行）和 `sync.json`（预热情况，以及选择器如何回答测量的轮次）。使用 `--similar` 时，主机提供 70 个 holdout 技能和它自带的技能；不安装目录。要把一个选择器加入已有的运行，只运行它的实验组（`--arms third_party`），并使用相同的 `--similar`、`--per-lane`、`--seed` 和模型。这样它的行可以与之前的行逐个提示词配对。

在测量的轮次之前，测试工具会检查两个提示词；如果两者都没有得到提示，它就会停止（"no prompt got a hint"）。在当前阈值下几乎处处回答 `no_match` 的选择器会在这里停止，没有运行的选择器也一样。真实轮次中网关的时间限制固定为 12 秒。

**5. 解读结果。**

- 先看 **Macro P 和 Macro R**，包括整体和 "By language" 下的数值。P wins/losses 和 R wins/losses 说明差异是否超过噪声。一个提高其中一个、降低另一个的改动是取舍，不是提升。
- 同时看无关提示词上的误选。对每个提示词都选点什么的选择器召回率很高，但会在这里失败。
- 在真实轮次中，把每种语言的正确技能数与两次默认运行比较。两次默认运行本身就在若干提示词上不同。统计得到 `no_match` 的轮次（`sync.json` 中的 `measured_selects`：预热之后每轮一次选择，包括检查提示词，因为它们也是测量的轮次）：有引导的实验组会隐藏技能列表，所以这样的轮次既没有提示也没有列表。
- 在同时运行选择器所用模型的同一台机器上测得的选择延迟不是速度结果。这些数据和期望答案由模型编写，所以结果只是开发诊断，不满足 AC25 到 AC37。

### 示例：使用本地嵌入的 asset-router

asset-router 自己不计算嵌入。它调用兼容 OpenAI 的 `{embeddings.baseUrl}/v1/embeddings`，把提示词与每个资产的描述比较。在回环地址上用 llama.cpp 的服务器提供嵌入模型，这样路由器会报告 `inference: local`，可以读取技能文件：

```bash
# llama.cpp b12237, llama-b12237-bin-ubuntu-x64.tar.gz,
#   sha256 ffd7ccfe4ca5f176f730988e049630ab0b943c5f1a1b005f1619a7048a745f68
# ggml-org/bge-m3-Q8_0-GGUF at 9eba04c5d75ba5a1595e45de734d36bef4e5cb98, bge-m3-q8_0.gguf,
#   sha256 aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173
llama-server -m bge-m3-q8_0.gguf --embeddings --pooling cls --host 127.0.0.1 --port 8950 \
  -c 8192 -b 8192 -ub 8192 -t 6
```

使用已经执行过 `npm ci` 的 asset-router 检出，例如 `scripts/asset_router_interop.py` 保存在 `~/.cache/relaymind-interop` 中的那个。每个实例在自己的工作目录中运行，并从该目录读取 `config/local.json`：

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

选择测试的实例不需要根目录。真实轮次使用端口 8941、另一个存储，以及 `"allowedRoots": ["/abs/turns/workspace/skills"]`。

**其他嵌入模型。** llama.cpp 能作为嵌入提供的任何模型都可以。设置 `embeddings.model`，把 `embeddings.dimensions` 设为模型的向量维度，并使用全新的存储，因为两个模型的向量不能混用。下面这些已经测量过（结果见 [EVIDENCE.md](EVIDENCE.md#2026-11-24--asset-router-with-four-embedding-models-rm47-ac42)）：

| 模型 | GGUF | `--pooling` | `dimensions` | 说明 |
| --- | --- | --- | ---: | --- |
| `bge-m3` | `ggml-org/bge-m3-Q8_0-GGUF`，见上 | `cls` | 1024 | asset-router 的 `minScore` 默认值是按这个模型设定的 |
| `ibm-granite/granite-embedding-97m-multilingual-r2`，`835ad14` | 未发布；转换为 F16 | `cls` | 384 | 9700 万参数 |
| `nomic-ai/nomic-embed-text-v2-moe-GGUF`，`ffbcf4c`，`nomic-embed-text-v2-moe.Q8_0.gguf`（sha256 `06e7a7e5…3cff`） | 已发布 | `mean` | 768 | 每段文本最多 512 个 token：用 `-c 2048 -np 4 -b 512 -ub 512` 启动；训练时使用 `search_query: ` 和 `search_document: ` 前缀，asset-router 不会发送 |
| `ThakiCloud/SKILLRET-Embedding-0.6B`，`67f1594` | 未发布；转换为 Q8_0 | `last` | 1024 | 针对英文技能检索微调；训练时查询带有 `Instruct: … Query:` 提示，asset-router 不会发送 |

要转换没有 GGUF 的模型，请按固定修订版下载，把它的 `model.safetensors` 与 Hugging Face 列出的 SHA-256 核对，再用同一个 llama.cpp 版本的转换脚本：

```bash
python <llama.cpp b12237 source>/convert_hf_to_gguf.py <model folder> --outtype q8_0 \
  --outfile model-q8_0.gguf
```

然后检查服务器的向量与原始模型一致。用 `sentence-transformers` 和 `/v1/embeddings` 分别嵌入几段英文和中文文本：每一对的余弦应不低于 0.99。错误的 `--pooling` 产生的向量看起来仍然合理，所以这一步不能省略。

**为每个模型设置 `minScore`。** asset-router 只保留得分高于 `minScore`（默认 0.55）的候选，而这个值是按 `bge-m3` 设定的。不同模型的分数尺度差别很大。在 holdout 上，正确技能与英文提示词的余弦中位数：`bge-m3` 为 0.60，granite 为 0.87，nomic 为 0.54，SKILLRET 为 0.42。因此在默认阈值下，granite 几乎全部保留，nomic 和 SKILLRET 几乎全部丢弃。换用其他模型时，先在不涉及选择器的情况下看它的分数尺度，以及仅靠嵌入的排序效果：

```bash
python scripts/guidance_embedding_probe.py --url http://127.0.0.1:8950 --split development \
  --output probe.json
```

它分别针对英文、中英混合和纯中文提示词，报告正确技能在该划分的技能中排第一或进入前 3 的次数，以及它的余弦（最小值、中位数、最大值）。`--query-prefix` 和 `--document-prefix` 可以显示模型的提示前缀会带来什么。然后在路由器配置中设置阈值，在 `--split development` 上按宏平均精确率和宏平均召回率调整，再只运行一次 holdout：

```json
"router": {"selected": "router.hybrid-bm25", "routers": {"router.hybrid-bm25": {"minScore": 0.40}}}
```

asset-router 的 BM25 阶段在每个 token 中只保留 `[a-z0-9]`，并且除非 `recallUnmatched` 为 true，只有 BM25 命中的资产才会成为候选。所以没有拉丁字母单词的提示词无论用哪个嵌入模型都会得到 `no_match`。
