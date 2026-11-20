tags: `[+]` fact, `[!]` current issue, `[req]` requirement

## Current State

### Guidance plugin

#001 `[+]` The guidance plugin adds a skill or tool hint and, in `strict` mode, enforces the selection limit. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L576).

#002 `[+]` The guidance plugin keeps per-session state only in `strict` mode. It has no persistent asset catalog; it has small shared in-memory offer and file caches. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L563).

#003 `[+]` One guidance plugin instance runs per OpenClaw host process. Sources: [openclaw.plugin.json](../integrations/openclaw/guidance-plugin/openclaw.plugin.json#L5).

#004 `[+]` The plugin must read the host skill snapshot on each prompt. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L593).

#005 `[+]` `toolsFile` is one plugin configuration and is the same for all sessions. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L503), [openclaw.plugin.json](../integrations/openclaw/guidance-plugin/openclaw.plugin.json#L15).

#091 `[+]` `toolsFile` is re-read when the file changes. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L112), [index.js](../integrations/openclaw/guidance-plugin/index.js#L503).

#095 `[+]` Guidance hooks are registered once at startup. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L576).

#065 `[+]` Per-session state holds the current offer, the strict limit, and used skills/tools. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L563).

#066 `[+]` One session can have a different skill set from another session. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L594).

#067 `[+]` Skill availability can differ per session; `toolsFile` tools are the same for all sessions. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L490), [index.js](../integrations/openclaw/guidance-plugin/index.js#L503).

#068 `[+]` Session state is in memory only. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L563).

### Catalog sources

#006 `[+]` The recommendation catalog uses `[[assets.sources]]`. Sources: [settings.py](../src/relaymind/assets/settings.py#L73), [asset-selection.md](../docs/asset-selection.md#L86).

#007 `[+]` A source must be `local_directory`, `metadata_import`, or `qualified_remote`. Sources: [settings.py](../src/relaymind/assets/settings.py#L111).

#008 `[+]` `[skills] roots` is not the recommendation catalog. Sources: [relaymind.example.toml](../relaymind.example.toml#L12), [launcher.py](../src/relaymind/launcher.py#L627).

#009 `[+]` Guidance assets come from the host request. Sources: [pipeline.py](../src/relaymind/guidance/pipeline.py#L178), [index.js](../integrations/openclaw/guidance-plugin/index.js#L490).

#064 `[+]` The recommendation catalog is the asset catalog built from `[[assets.sources]]` for the recommendations service. It is separate from guidance host assets and managed skills. Sources: [settings.py](../src/relaymind/assets/settings.py#L73), [asset-selection.md](../docs/asset-selection.md#L18).

### Contracts

#010 `[+]` Selection uses `SelectionRequestV2` and `SelectionResultV2`. Sources: [selection_request.v2.json](../src/relaymind/selection/schemas/selection_request.v2.json#L1), [selection_result.v2.json](../src/relaymind/selection/schemas/selection_result.v2.json#L1).

#011 `[+]` Asset recommendation preparation, when configured, uses `AssetPreparationRequestV1` and `AssetPreparationResponseV1`. Sources: [asset_preparation_request.v1.json](../src/relaymind/selection/schemas/asset_preparation_request.v1.json#L1), [asset_preparation_response.v1.json](../src/relaymind/selection/schemas/asset_preparation_response.v1.json#L1).

#012 `[+]` Managed Selection V1 is separate and is not used by the asset router. Sources: [asset-selection.md](../docs/asset-selection.md#L18), [contract.py](../src/relaymind/selection/contract.py#L59), [asset_contract.py](../src/relaymind/selection/asset_contract.py#L31).

#092 `[+]` V2 select is host-agnostic. Sources: [ASSET_BENCHMARK.md](../docs/ASSET_BENCHMARK.md#L53).

#070 `[+]` V1 routes one executable skill. Sources: [asset-selection.md](../docs/asset-selection.md#L20).

#071 `[+]` V1 uses `[selection]`. Sources: [asset-selection.md](../docs/asset-selection.md#L22), [settings.py](../src/relaymind/selection/settings.py#L34).

#072 `[+]` V2 recommends assets. Sources: [asset-selection.md](../docs/asset-selection.md#L20).

#073 `[+]` V2 uses `[recommendations]` or `[guidance]`. Sources: [asset-selection.md](../docs/asset-selection.md#L22), [guidance.md](../docs/guidance.md#L140).

### Asset identity

#013 `[+]` The catalog source sets `asset_id`: guidance uses `host:<kind>:<name>`, a local skill uses `skill:<digest of source and path>`, and a metadata import declares its ID. Sources: [pipeline.py](../src/relaymind/guidance/pipeline.py#L624), [sources.py](../src/relaymind/assets/sources.py#L609), [guidance.md](../docs/guidance.md#L227).

#014 `[+]` `source_id`, `source_locator`, and `source_version` are provenance. Sources: [asset_record.v1.json](../src/relaymind/store/schemas/asset_record.v1.json#L9).

#015 `[+]` `source_id` does not state the source kind. Sources: [asset_record.v1.json](../src/relaymind/store/schemas/asset_record.v1.json#L35), [settings.py](../src/relaymind/assets/settings.py#L111).

#016 `[+]` `source_locator` can be a local path, host URI, or declared URL. Sources: [sources.py](../src/relaymind/assets/sources.py#L615), [pipeline.py](../src/relaymind/guidance/pipeline.py#L624), [guidance.md](../docs/guidance.md#L227).

#018 `[+]` `AssetRecord` in source code has no `content` field. Sources: [catalog.py](../src/relaymind/assets/catalog.py#L163).

#046 `[+]` Some assets have no natural locator: guidance host assets, memory, hardcoded data, and private stores. `AssetRecordV1` still requires a nonempty `source_locator`. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L490), [pipeline.py](../src/relaymind/guidance/pipeline.py#L624), [asset_record.v1.json](../src/relaymind/store/schemas/asset_record.v1.json#L39).

#048 `[+]` The guidance plugin builds each asset as `{name, kind, description}` only. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L490).

#049 `[+]` The guidance pipeline synthesizes `source_locator = host://<kind>/<name>`. Sources: [pipeline.py](../src/relaymind/guidance/pipeline.py#L624).

#074 `[+]` `package_id` is the skill name from the `SKILL.md` frontmatter. Sources: [sources.py](../src/relaymind/assets/sources.py#L645).

#075 `[+]` `package_id` does not contain the path or the content hash. Sources: [sources.py](../src/relaymind/assets/sources.py#L645).

#093 `[+]` Skill identity includes `package_version`. Sources: [sources.py](../src/relaymind/assets/sources.py#L645).

#094 `[+]` Tool identity includes `exposed_name` and `schema_hash`. Sources: [asset_record.v1.json](../src/relaymind/store/schemas/asset_record.v1.json#L91), [pipeline.py](../src/relaymind/guidance/pipeline.py#L605).

### Selection and preparation

#019 `[+]` `SelectionRequestV2` sends full `AssetRecordV1` metadata records. They contain no full text. Sources: [selection_request.v2.json](../src/relaymind/selection/schemas/selection_request.v2.json#L127).

#020 `[+]` `AssetPreparationRequestV1` sends `AssetRecordV1` metadata. Sources: [asset_preparation.py](../src/relaymind/selection/asset_preparation.py#L329).

#021 `[+]` Preparation V1 binds an index to one catalog revision. Sources: [asset_preparation_request.v1.json](../src/relaymind/selection/schemas/asset_preparation_request.v1.json#L29), [asset_preparation.py](../src/relaymind/selection/asset_preparation.py#L329).

### Router

#026 `[+]` The selector URL must be loopback HTTP. Sources: [http.py](../src/relaymind/selection/http.py#L27), [asset_http.py](../src/relaymind/selection/asset_http.py#L101).

#027 `[+]` The catalog limit is 1000 assets per prepare and per selection request. Sources: [selection_request.v2.json](../src/relaymind/selection/schemas/selection_request.v2.json#L296), [asset_preparation_request.v1.json](../src/relaymind/selection/schemas/asset_preparation_request.v1.json#L344).

#029 `[+]` The host owns execution, approvals, and permissions. Sources: [AGENTS.md](../AGENTS.md#L38).

### Tools

#032 `[+]` A guidance asset must be a skill or an `mcp_tool`. Sources: [pipeline.py](../src/relaymind/guidance/pipeline.py#L178).

#033 `[+]` A tool asset uses `server_id` and `tool_name` in its identity. Sources: [asset_record.v1.json](../src/relaymind/store/schemas/asset_record.v1.json#L91), [pipeline.py](../src/relaymind/guidance/pipeline.py#L605).

#087 `[+]` A tool full text is `description` plus `input_schema`; execution stays in the host. Sources: [asset_source_catalog.py](../src/relaymind/evaluation/asset_source_catalog.py#L98), [AGENTS.md](../AGENTS.md#L38).

### Hashes

#034 `[+]` Recommendation catalog `content_hash` for a local skill is SHA-256 of the `SKILL.md` bytes. Sources: [sources.py](../src/relaymind/assets/sources.py#L615).

#035 `[+]` Recommendation catalog `content_hash` for a source-built skill is SHA-256 of the original bytes. Sources: [asset_source_catalog.py](../src/relaymind/evaluation/asset_source_catalog.py#L56).

#036 `[+]` Metadata import `content_hash` hashes the file at `original_path`. Sources: [guidance.md](../docs/guidance.md#L227), [sources.py](../src/relaymind/assets/sources.py#L833).

#037 `[+]` A tool identity has `schema_hash` for `input_schema`. Sources: [asset_source_catalog.py](../src/relaymind/evaluation/asset_source_catalog.py#L98).

#038 `[+]` Guidance V2 `content_hash` is SHA-256 of the projected `description` only. Sources: [pipeline.py](../src/relaymind/guidance/pipeline.py#L627).

#039 `[+]` Guidance V2 does not hash the skill body. Sources: [pipeline.py](../src/relaymind/guidance/pipeline.py#L627).

#040 `[+]` The guidance offer sends no file mtime and no body bytes. Sources: [index.js](../integrations/openclaw/guidance-plugin/index.js#L490), [pipeline.py](../src/relaymind/guidance/pipeline.py#L624).

#063 `[+]` In a valid recommendation catalog, a body-only change gives a new `content_hash`: local and source-built catalogs compute it; a metadata import must be regenerated, or capture rejects the mismatch. Sources: [sources.py](../src/relaymind/assets/sources.py#L615), [sources.py](../src/relaymind/assets/sources.py#L833), [asset_source_catalog.py](../src/relaymind/evaluation/asset_source_catalog.py#L56).

## Requirements

#022 `[req]` An authoritative-index router requires a ready prepared revision for the requested catalog revision.

#023 `[req]` The router must reject a stale or unknown catalog revision.

#024 `[req]` The router filters candidates to the selection catalog.

#025 `[req]` The router returns selection catalog `asset_id`, `content_hash`, and `source_version`.

#028 `[req]` The router is session-agnostic and keeps one shared index.

#030 `[req]` The router ranks only. It executes nothing and grants nothing.

#076 `[req]` The per-request catalog is the authority for that request.

#077 `[req]` The selection catalog can be a session-specific subset of the prepared catalog.

#078 `[req]` The router must bind each `catalog_revision` to one prepared `catalog_digest` and reject an unknown or ambiguous revision.

#079 `[req]` Cache mode uses the offer as authority and needs no Preparation V1.

#080 `[req]` Authoritative-index mode needs Preparation V1.

#081 `[req]` The router must key cache entries by identity plus `content_hash`, not by session.

#082 `[req]` The router must update a cache entry when `content_hash` changes.

#083 `[req]` A cache cannot share entries between recommendation and guidance by `content_hash` alone; use identity plus the source-specific hash.

#084 `[req]` More than 1000 assets needs a split or a contract extension.

#085 `[req]` A remote host is blocked by the loopback rule.

#086 `[req]` Any full-text transfer must bind to `content_hash`.

#088 `[req]` A new host plugin can reuse the router service on another port as a separate instance.

#089 `[req]` A new host must build valid `AssetRecordV1` items for each skill and tool.

## Current Issues

#041 `[!]` The router needs full text, but V2 and Preparation V1 send only projected records.

#042 `[!]` No stable, common source access contract exists today.

#043 `[!]` The router cannot read a source that exists only in the plugin's memory.

#047 `[!]` A synthetic `source_locator` is only a label. It does not make the source readable. The router still cannot read it, so the full text must come another way.

#044 `[!]` In guidance V2, a body-only change changes no `content_hash`.

#045 `[!]` The guidance plugin sends no body hash or mtime.

#050 `[!]` RelayMind asset IDs are source-defined (`host:skill:<name>`, `skill:<digest>`, or an import ID). Asset-router IDs are local index IDs (`skill:<name>`). The same asset can have different IDs.

#051 `[!]` Name-based router IDs collide when two RelayMind sources use the same skill name.

#052 `[!]` The same asset has different `content_hash` values in recommendation and guidance.

#053 `[!]` RelayMind and the asset router use separate asset sources.

#054 `[!]` The current asset router has no preparation revision binding.

## Resolutions

#055 resolve: #041, #042, #043, #047: add a `content` payload (`inline` text or `file` path) only to `AssetPreparationRequestV1`. `SelectionRequestV2` stays unchanged and contains no content. Full text or a readable path goes in preparation. Memory and private sources use `inline`. No HTTP source.

#056 resolve: #044, #045: make `content_hash` cover the full body and send a body hash or body in the offer.

#057 resolve: #050, #051: the host assigns stable unique asset IDs. The router treats IDs as opaque and builds its map from the received catalog. Name collisions are the host's responsibility.

#058 resolve: #038, #052, #063: use the body hash for all paths. Guidance must stop using the description-only hash. Recommendation already hashes the full body.

#059 resolve: #053: the host prepares the exact RelayMind catalog. The router indexes that catalog and filters to the selection catalog.

#060 resolve: #054: add Preparation V1 for authoritative-index mode. A cache-mode router uses the offer as authority and needs no preparation.

#061 resolve: #028, #030: session management is not the router's responsibility. The host owns sessions. The router only prepares an index projection from the provided catalog.

#062 resolve: #052, #053, #054: RelayMind must check for stale assets before selection. If any asset changed, RelayMind must send `prepare` again. Selection must use only a ready revision.
