"""Durable public-asset vectors with exact scoped compatibility (RM38/RM41).

Task/history representations and adaptation vectors never enter this table.
"""

import json

from relaymind.assets.embeddings import EmbeddingError
from relaymind.canonical import canonical_digest, canonical_json
from relaymind.selection.asset_hybrid import HybridIndex


class AssetVectorStore:
    def __init__(self, store):
        self.store = store
        with store.sql_transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS asset_vectors (
                vector_key TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                scope_json TEXT NOT NULL, config_digest TEXT NOT NULL,
                vector_json TEXT NOT NULL, vector_digest TEXT NOT NULL
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS asset_vectors_scope ON asset_vectors(scope_json)"
            )

    def save_batch(self, index, records, vectors, *, check=lambda: None) -> None:
        staged = HybridIndex(index.config, index.scope)
        staged.publish_batch(records, vectors, check=check)
        scope = canonical_json(index.scope)
        with self.store.sql_transaction() as db:
            for record in records:
                check()
                vector = staged.compatible(record)
                db.execute(
                    "INSERT INTO asset_vectors VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(vector_key) DO UPDATE SET vector_json=excluded.vector_json, "
                    "vector_digest=excluded.vector_digest",
                    (
                        staged.key(record),
                        record["asset_id"],
                        scope,
                        index.config.digest,
                        canonical_json(vector),
                        canonical_digest(vector),
                    ),
                )
                check()

    _SQL_BATCH_SIZE = 250

    @classmethod
    def _batches(cls, items):
        for start in range(0, len(items), cls._SQL_BATCH_SIZE):
            yield items[start : start + cls._SQL_BATCH_SIZE]

    def restore(self, config, scope, records, *, check=lambda: None) -> HybridIndex:
        index = HybridIndex(config, scope)
        scope_json = canonical_json(scope)
        found_records, found_vectors = [], []
        keyed = []
        for record in records:
            check()
            keyed.append((index.key(record), record))
        for chunk in self._batches(keyed):
            keys = list(dict.fromkeys(k for k, _ in chunk))
            placeholders = ",".join("?" for _ in keys)
            check()
            with self.store.sql_read() as db:
                rows = db.execute(
                    "SELECT vector_key,asset_id,length(vector_json),"
                    "substr(vector_json,1,131073),vector_digest "
                    f"FROM asset_vectors WHERE scope_json=? AND config_digest=? "
                    f"AND vector_key IN ({placeholders})",
                    (scope_json, config.digest, *keys),
                ).fetchall()
            check()
            by_key = {row[0]: row for row in rows}
            for vector_key, record in chunk:
                row = by_key.get(vector_key)
                if row is None:
                    continue
                if row[1] != record["asset_id"] or row[2] > 131072:
                    raise EmbeddingError("corrupt_vector")
                try:
                    vector = json.loads(row[3])
                    if canonical_digest(vector) != row[4]:
                        raise EmbeddingError("corrupt_vector")
                except (ValueError, TypeError, RecursionError):
                    raise EmbeddingError("corrupt_vector") from None
                found_records.append(record)
                found_vectors.append(vector)
                if len(found_records) == 25:
                    index.publish_batch(found_records, found_vectors, check=check)
                    found_records, found_vectors = [], []
        if found_records:
            index.publish_batch(found_records, found_vectors, check=check)
        return index

    def prune(self, config, scope, records, *, check=lambda: None) -> None:
        index = HybridIndex(config, scope)
        allowed = {index.key(record) for record in records}
        with self.store.sql_transaction() as db:
            rows = db.execute(
                "SELECT vector_key FROM asset_vectors WHERE scope_json=?",
                (canonical_json(scope),),
            ).fetchall()
            stale = [row[0] for row in rows if row[0] not in allowed]
            for batch in self._batches(stale):
                check()
                placeholders = ",".join("?" for _ in batch)
                db.execute(
                    f"DELETE FROM asset_vectors WHERE vector_key IN ({placeholders})",
                    batch,
                )
            check()
