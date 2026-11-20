# Asset router protocol

## Purpose

This protocol connects RelayMind to a local asset router.

- RelayMind owns the catalog and IDs.
- The router owns only the index and ranking.
- In authoritative-index mode, full text goes in `prepare`.
- Selection stays lean.

## Modes

- Cache mode: the router uses the selection catalog as authority. No preparation.
- Authoritative-index mode: RelayMind prepares the full catalog. Selection uses a ready revision.

## Artifacts

| Artifact | Producer | Consumer |
| --- | --- | --- |
| `AssetRecordV1` | RelayMind | RelayMind |
| Prepared record | RelayMind | Router |
| `AssetPreparationRequestV1` | RelayMind | Router |
| `AssetPreparationResponseV1` | Router | RelayMind |
| `SelectionRequestV2` | RelayMind | Router |
| `SelectionResultV2` | Router | RelayMind |
| Router index | Router | Router |

## Flow

1. RelayMind builds the catalog and assigns stable IDs.
2. RelayMind computes `content_hash`, `schema_hash`, revision, and digest.
3. In authoritative-index mode, RelayMind sends `prepare` with the prepared catalog.
4. The router builds or reuses the index and returns `index_revision`.
5. RelayMind builds the selection catalog.
6. RelayMind sends `select` with the revision and the lean catalog.
7. In authoritative-index mode, the router rejects a stale revision.
8. The router filters to the selection catalog and ranks.
9. RelayMind validates the result.

## Invariants

- In authoritative-index mode, one prepared revision binds one catalog digest.
- In authoritative-index mode, one catalog revision has one ready `index_revision`.
- `SelectionRequestV2` contains no full text.
- Prepared records contain `inline` text or a `file` path.
- The router never executes, grants, or changes a budget.
- The router never keeps session state.
- The router returns only selection catalog IDs and hashes.
- A body change produces a new `content_hash`.
- In authoritative-index mode, a stale revision is rejected before ranking.

## Files

- [Protocol analysis](protocol-analysis.md)
- [RelayMind requirements](requirements.md)
- [Router requirements](router-requirements.md)
- [Asset record schema](schemas/asset_record.v1.json)
- [Preparation request schema](schemas/asset_preparation_request.v1.json)
