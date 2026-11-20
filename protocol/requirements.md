# RelayMind requirements

## Goal

RelayMind owns the asset catalog, sessions, identity, and execution.
It prepares the asset router with one exact catalog revision.
It sends a lean selection request for each prompt.

## Story

The operator starts RelayMind with `[[assets.sources]]`.
RelayMind builds an `AssetRecordV1` for each skill and tool.
It assigns stable IDs, hashes, and one catalog revision.
In authoritative-index mode, it sends the full catalog with
`AssetPreparationRequestV1`, and the router returns a ready `index_revision`.
On a prompt, RelayMind builds the selection catalog subset.
It sends `SelectionRequestV2` with that subset.
The router filters, ranks, and returns candidates.
RelayMind validates the result and shows a hint.
The host executes the selected assets under its own rules.

## Modes

- Cache mode: the router uses the selection catalog as authority. RelayMind sends no preparation.
- Authoritative-index mode: RelayMind prepares the full catalog. Selection uses a ready revision.

## Artifacts

| Artifact | Producer | Consumer | Content |
| --- | --- | --- | --- |
| Catalog snapshot | RelayMind | RelayMind | `AssetRecordV1` metadata |
| Prepared catalog | RelayMind | Router | Metadata plus `content` |
| `AssetPreparationRequestV1` | RelayMind | Router | Binding, profile, prepared catalog, budget |
| `AssetPreparationResponseV1` | Router | RelayMind | State, `index_revision`, reuse, reason |
| `SelectionRequestV2` | RelayMind | Router | Task, scope, revision, lean catalog |
| `SelectionResultV2` | Router | RelayMind | Candidates, `index_revision`, decision |
| Router index | Router | Router | Assets keyed by identity plus `content_hash` |

## Catalog

- RelayMind MUST build the catalog from `[[assets.sources]]`.
- `[skills] roots` MUST NOT be the recommendation catalog.
- A source MUST be `local_directory`, `metadata_import`, or `qualified_remote`.
- RelayMind MUST keep the source-defined `asset_id` stable and unique.
- A content change MUST keep the same `asset_id`.
- RelayMind MUST increase `catalog_revision` when an asset is added, removed, or changed.
- RelayMind MUST compute `catalog_digest` over the prepared catalog in authoritative-index mode.
- RelayMind MUST enforce the 1000-asset limit.
- More than 1000 assets MUST be split or need a contract extension.

## Preparation

- In authoritative-index mode, RelayMind MUST send `AssetPreparationRequestV1` before the first selection.
- In authoritative-index mode, RelayMind MUST send `prepare` again when any asset changes.
- In authoritative-index mode, RelayMind MUST check for stale assets before each selection.
- RelayMind MUST send full text or a readable file path in `prepare` only.
- RelayMind MUST use `content.kind=inline` for memory, hardcoded, or private sources.
- RelayMind MUST use `content.kind=file` for a shared local file.
- RelayMind MUST NOT send an HTTP source.
- RelayMind MUST bind the prepared catalog to selector identity, scope, permissions, kinds, revision, and digest.
- In authoritative-index mode, RelayMind MUST NOT use a stale prepared revision for selection.

## Selection

- The selector URL MUST be loopback HTTP.
- RelayMind MUST send `SelectionRequestV2`.
- The selection catalog MUST be metadata only.
- The selection catalog MUST NOT contain `content`.
- The selection catalog MAY be a subset of the prepared catalog (e.g. session-specific catalog).
- RelayMind MUST send the current `catalog_revision`.
- RelayMind MUST send the task, conversation, scope, permissions, kinds, and deadline.
- RelayMind MUST enforce the 1000-asset limit.
- In authoritative-index mode, RelayMind MUST use only a ready revision.

## Result validation

- RelayMind MUST check selector name and version.
- RelayMind MUST check `recommendation_id` and `catalog_revision`.
- In authoritative-index mode, RelayMind MUST check `index_revision` against the prepared revision.
- RelayMind MUST accept only candidates in the selection catalog.
- RelayMind MUST check `asset_id`, `kind`, `content_hash`, and `source_version`.
- RelayMind MUST ignore an invalid result.
- RelayMind MUST fail open: no hint and no limit on error.

## Change detection

- The recommendation catalog MUST hash the full asset body.
- A local skill MUST hash the `SKILL.md` bytes.
- A source-built skill MUST hash the original bytes.
- A metadata import MUST regenerate its hash after a body change, or capture MUST reject the mismatch.
- A tool MUST hash `{name, description, input_schema}` and also provide `schema_hash`.
- Guidance MUST use a body hash, not the description-only hash.
- A body-only change MUST produce a new `content_hash`.
- A schema-only change MUST produce a new `schema_hash` and `content_hash`.
- Any full-text transfer MUST bind to `content_hash`.

## Identity

- The catalog source sets `asset_id`.
- Guidance MUST use `host:<kind>:<name>`.
- A local skill MUST use `skill:<digest of source and path>`.
- A metadata import MUST declare its own ID.
- Skill identity MUST include `package_id` and `package_version`.
- Tool identity MUST include `server_id`, `tool_name`, `exposed_name`, `input_schema`, and `schema_hash`.
- RelayMind MUST keep IDs unique. The router treats them as opaque.
- A name collision is a RelayMind problem, not a router problem.

## Sessions and execution

- RelayMind MUST own session state.
- RelayMind MUST own execution, approvals, and permissions.
- The router MUST NOT receive session state.
- The host MUST execute the selected assets.
- The router MUST rank only.

## Failure

- If preparation fails, RelayMind MUST send no hint.
- If selection times out, RelayMind MUST send no hint.
- If the result is invalid, RelayMind MUST send no hint.
- In `strict` mode, a failure MUST NOT add a limit.
- RelayMind MUST NOT replace a failed result with a stale result.

## New host

- A new host MUST build valid `AssetRecordV1` items for each skill and tool.
- A new host MUST compute body hashes and tool schema hashes.
- A new host MAY reuse the router service on another loopback port as a separate instance.

## Changes to implement

1. Add the `content` union to prepared records only.
2. Keep `SelectionRequestV2` unchanged and lean.
3. Add the stale check and re-prepare step before selection.
4. Move guidance to a full-body `content_hash`.
5. Add the ID map from the received catalog.
6. Keep execution and session state in RelayMind.
