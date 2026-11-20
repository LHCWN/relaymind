# Asset router requirements

## Transport

- Serve plain HTTP on loopback only.
- Refuse a URL with user info, a query, or a fragment.
- Accept strict JSON. Reject duplicate keys, NaN, Infinity, and oversized input.
- Do not follow redirects, use proxies, retry, or accept credentials.
- Serve `POST /prepare` and `POST /select`.
- Serve `GET /readyz`.
- A remote host is out of scope.

## Modes

- Cache mode: use the selection catalog as authority. No preparation is required.
- Authoritative-index mode: require a ready prepared revision.

## Prepare

- In authoritative-index mode, validate the binding, catalog, revision, and digest.
- Reject a catalog above 1000 assets.
- Read a `file` source or use inline `content`.
- Verify `content_hash` after each read.
- Build one index for the exact catalog revision.
- Return `index_revision` only when ready.
- Reuse a ready index for the same binding.
- Keep `status` and `cancel` bound to the same `preparation_id`.
- Do not build during `status` or `cancel`.

## Select

- In authoritative-index mode, require a ready index for `catalog_revision`.
- Reject a stale, unknown, or ambiguous revision.
- Reject a selection catalog asset that is not prepared in authoritative-index mode.
- Verify each catalog `asset_id`, `kind`, and `content_hash`.
- Filter every candidate to the selection catalog.
- Return only selection catalog IDs and hashes.
- Do not use name matching.
- Return `no_match` when the selection catalog is empty.

## Index

- Key entries by identity plus `content_hash`.
- Do not share an entry between recommendation and guidance by `content_hash` alone. Use identity plus the source-specific hash.
- Update an entry when `content_hash` changes.
- Remove an entry that is absent from the new revision.
- Keep one shared index across all sessions.
- Do not mix assets from different revisions.
- Keep a ready index while a new revision builds.
- Reject a select for the revision under construction.

## Result

- Echo `recommendation_id` and `catalog_revision`.
- Return the prepared `index_revision`.
- Return selector name, version, and mechanism.
- Return a score and `score_kind` for each candidate.
- Return JSON Pointers or empty `evidence_refs`.
- Bound the response to 1 MiB.
- Answer within `deadline_ms`.

## Safety

- Do not keep session state.
- Do not execute a skill or tool.
- Do not grant a permission.
- Do not change a budget.
- Do not return an asset outside the selection catalog.
- Do not log prompt text, source text, or secrets.
- Return a structured error with a safe message.
