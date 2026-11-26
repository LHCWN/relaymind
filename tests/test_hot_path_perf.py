"""Hot-path performance regression tests (#295; RM27, RM28, RM32, RM37, RM38, RM42, RM45)."""

import asyncio
import json
import threading
from datetime import timedelta

import pytest

from helpers import FakeClock
from relaymind.assets.catalog import validate_asset_record
from relaymind.canonical import canonical_digest
from relaymind.guidance import pipeline as guidance_pipeline
from relaymind.orchestrator.placement import Placement
from relaymind.recommendations.admission import current_asset
from relaymind.selection.catalog import catalog_entries
from relaymind.skills import predicates
from relaymind.store import ModelTiming, RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from relaymind.store.asset_vectors import AssetVectorStore
from relaymind.tools import ToolResult, ToolStatus
from test_asset_catalog import record as asset_record_dict
from test_asset_hybrid import setup as hybrid_setup


def _skill_record(index: int) -> dict:
    description = f"Skill number {index} description"
    return {
        "schema_version": 1,
        "asset_id": f"src:skill:s-{index}",
        "kind": "skill",
        "source_id": "src",
        "source_locator": f"file:///skills/s-{index}/SKILL.md",
        "source_version": "v1",
        "captured_at": "2026-10-20T00:00:00Z",
        "content_hash": canonical_digest({"id": index}),
        "description": description,
        "language_tags": ["en"],
        "dependencies": [],
        "availability": "available",
        "stale": False,
        "identity": {"package_id": f"s-{index}", "package_version": "1.0.0"},
    }


def test_current_asset_performs_one_indexed_row_read(tmp_path, monkeypatch):
    """RM37, #295: recommendation admission reads one asset row instead of the full catalog."""
    from relaymind.assets.catalog import CatalogUnavailable

    store = RunStore(":memory:", clock=FakeClock())
    try:
        catalog = AssetCatalogStore(store)
        records = [validate_asset_record(_skill_record(i)) for i in range(10)]
        with store.sql_transaction() as db:
            db.execute(
                "INSERT INTO asset_catalog_sources (source_id, stale, complete)"
                " VALUES ('src', 0, 1)"
            )
            for rec in records:
                from relaymind.assets.catalog import projection_digest

                digest = projection_digest(rec, None)
                search_json = json.dumps({"body": None, "projection_digest": digest})
                db.execute(
                    "INSERT INTO asset_catalog_assets"
                    " (asset_id, source_id, record_json, search_json, projection_digest, stale)"
                    " VALUES (?, ?, ?, ?, ?, 0)",
                    (
                        rec.asset_id,
                        rec.source_id,
                        json.dumps(rec.to_json_object()),
                        search_json,
                        digest,
                    ),
                )

        doc_calls = 0
        real_document = catalog._document

        def counted_document(row):
            nonlocal doc_calls
            doc_calls += 1
            return real_document(row)

        monkeypatch.setattr(catalog, "_document", counted_document)

        queries: list[str] = []
        store._conn.set_trace_callback(queries.append)
        try:
            found = current_asset(catalog, "src:skill:s-5")
            missing = current_asset(catalog, "src:skill:missing")
        finally:
            store._conn.set_trace_callback(None)

        assert found is not None and found.asset_id == "src:skill:s-5"
        assert missing is None
        assert doc_calls == 1, f"expected 1 decoded asset row, got {doc_calls}"
        selects = [q for q in queries if q.lstrip().upper().startswith("SELECT")]
        row_selects = [q for q in selects if "WHERE a.asset_id" in q]
        assert len(row_selects) == 2, f"expected one keyed asset SELECT per lookup, got {selects}"
        assert not any("SELECT * FROM asset_catalog_assets" in q for q in selects), selects
        assert not any("json_extract" in q for q in selects), selects

        # Verify SQLite uses primary-key SEARCH with no full-table SCAN on asset_catalog_assets.
        plan = [
            row["detail"]
            for row in store._conn.execute(
                "EXPLAIN QUERY PLAN SELECT a.*, s.stale AS source_stale"
                " FROM asset_catalog_assets a"
                " LEFT JOIN asset_catalog_sources s ON s.source_id = a.source_id"
                " WHERE a.asset_id = ?",
                ("src:skill:s-5",),
            ).fetchall()
        ]
        assert not any(step.startswith("SCAN ") for step in plan), plan

        # Subclass overriding snapshot() must still route through snapshot().
        class SubclassCatalog(AssetCatalogStore):
            snapshot_calls = 0

            def snapshot(self):
                self.snapshot_calls += 1
                return super().snapshot()

        sub = SubclassCatalog(store)
        with store.sql_transaction() as db:
            db.execute(
                "UPDATE asset_catalog_sources SET source_version='v1', content_hash=?,"
                " captured_at='2026-10-20T00:00:00Z', last_success_at='2026-10-20T00:00:00Z',"
                " accepted_count=10, rejected_count=0, exclusions_json='[]'"
                " WHERE source_id='src'",
                (canonical_digest({"src": "v1"}),),
            )
        assert current_asset(sub, "src:skill:s-5") is not None
        assert sub.snapshot_calls == 1

        # Stale projection mismatch on the target row fails closed.
        with store.sql_transaction() as db:
            db.execute("UPDATE asset_catalog_assets SET stale = 1 WHERE asset_id = 'src:skill:s-5'")
        with pytest.raises(CatalogUnavailable, match="catalog_projection_unavailable"):
            current_asset(catalog, "src:skill:s-5")
    finally:
        store.close()


def test_catalog_entries_does_not_reparse_examples_json(tmp_path, monkeypatch):
    """RM28, #295: routing reads pre-parsed examples from Skill without calling json.loads."""
    from gateway_helpers import build_gateway

    registry = build_gateway(tmp_path, []).skills
    names = list(registry.names())

    loads_calls = 0
    real_loads = json.loads

    def counted_loads(*args, **kwargs):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(json, "loads", counted_loads)
    entries = catalog_entries(registry, names)
    assert any(entry["examples"] for entry in entries)
    assert loads_calls == 0


def test_placement_fetches_timing_samples_once_and_caches_by_latest_sample(monkeypatch):
    """RM27, #295: Placement.predict fetches model timings once and caches when unchanged."""
    clock = FakeClock()
    store = RunStore(":memory:", clock=clock)
    try:

        def add_sample(output_tokens: int) -> None:
            store.add_model_timing(
                ModelTiming(
                    model="local",
                    actor="local",
                    purpose="routing",
                    kind="run",
                    input_tokens=100,
                    cached_tokens=0,
                    first_token_ms=20,
                    duration_ms=100,
                    output_tokens=output_tokens,
                    succeeded=True,
                    run_id=None,
                    recorded_at=clock.utc_now(),
                )
            )

        add_sample(40)
        placement = Placement(store)

        calls = 0
        real_timings = store.model_timings

        def counted_timings(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_timings(*args, **kwargs)

        monkeypatch.setattr(store, "model_timings", counted_timings)

        first = placement.predict("local", "routing", 200, output_cap=128)
        assert calls == 1, f"expected 1 model_timings fetch, got {calls}"
        assert first.inputs["expected_output_tokens"] == 40
        assert type(first.inputs["expected_output_tokens"]) is int

        second = placement.predict("local", "routing", 200, output_cap=128)
        assert second.predicted_ms == first.predicted_ms
        assert calls == 1, f"expected cached reuse on unchanged samples, got {calls}"

        clock.advance(1)
        add_sample(80)
        third = placement.predict("local", "routing", 200, output_cap=128)
        assert calls == 2
        assert third.inputs["expected_output_tokens"] == 60

        # Advancing past retention invalidates the cache.
        clock.advance(timedelta(days=8).total_seconds())
        fourth = placement.predict("local", "routing", 200, output_cap=128)
        assert fourth.predicted_ms is None
    finally:
        store.close()


def test_vector_restore_and_prune_batch_sql_queries(tmp_path):
    """RM38, #295: AssetVectorStore restore and prune batch queries instead of N+1."""
    config, index = hybrid_setup(tmp_path)
    store = RunStore(":memory:", clock=FakeClock())
    try:
        vec_store = AssetVectorStore(store)
        records = [
            asset_record_dict(asset_id=f"src:skill:s-{i}", description=f"Skill {i}")
            for i in range(60)
        ]
        vectors = [(1.0, 0.0) for _ in range(60)]
        for start in range(0, len(records), 25):
            vec_store.save_batch(index, records[start : start + 25], vectors[start : start + 25])

        restore_queries: list[str] = []
        store._conn.set_trace_callback(restore_queries.append)
        try:
            restored = vec_store.restore(config, index.scope, records)
        finally:
            store._conn.set_trace_callback(None)

        assert all(restored.compatible(r) is not None for r in records)
        selects = [q for q in restore_queries if q.lstrip().upper().startswith("SELECT")]
        assert len(selects) <= 2, f"expected batched SELECTs, got {len(selects)}"

        prune_queries: list[str] = []
        store._conn.set_trace_callback(prune_queries.append)
        try:
            vec_store.prune(config, index.scope, records[:10])
        finally:
            store._conn.set_trace_callback(None)

        deletes = [q for q in prune_queries if q.lstrip().upper().startswith("DELETE")]
        assert len(deletes) <= 2, f"expected batched DELETEs, got {len(deletes)}"
    finally:
        store.close()


def test_adaptation_stage_reuses_snapshot_when_unchanged(tmp_path, monkeypatch):
    """RM42, #295: adaptation filters in SQL and guard avoids re-decoding unchanged history."""
    from recommendation_helpers import BODY
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.adaptation_stage import AdaptationStage
    from test_asset_adaptation_stage import config as adaptation_config
    from test_asset_adaptation_stage import request_for
    from test_asset_adaptation_store import correct
    from test_asset_user_actions import action_client
    from test_embedding_backend import NativeDouble

    async def exercise():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (
            client,
            app,
            service,
            runs,
            catalog,
            _,
        ):
            parent = (
                await client.post(
                    "/relay/v1/recommendations",
                    json=BODY,
                    headers={"Idempotency-Key": "parent-2"},
                )
            ).json()
            await correct(client, parent, 0, "first")
            semantic = adaptation_config(tmp_path)
            stage = AdaptationStage(
                app.state.feedback_store,
                semantic,
                EmbeddingEngine(semantic.manifest, NativeDouble()),
            )
            primary = parent["result"]["attempts"][0]["result"]
            req = request_for(service, catalog)

            snapshot_calls = 0
            observed_records: list[dict] = []
            real_snap = app.state.feedback_store.correction_snapshot

            def counted_snap(*args, **kwargs):
                nonlocal snapshot_calls
                snapshot_calls += 1
                snap = real_snap(*args, **kwargs)
                observed_records.extend(snap.records)
                return snap

            monkeypatch.setattr(app.state.feedback_store, "correction_snapshot", counted_snap)
            outcome = await stage.apply(
                primary,
                req,
                budget=service.new_budget(),
                current_catalog=service.provider.current,
            )
            outcome.guard()
            outcome.close()
            stage.close()
            assert snapshot_calls == 1, f"expected 1 correction_snapshot call, got {snapshot_calls}"
            assert len(observed_records) == 1
            assert observed_records[0]["feedback"]["kind"] == "correction"
            assert isinstance(observed_records[0]["retain_until"], str)
            assert outcome.adaptation["feedback_revision"] == 1
            assert len(outcome.embeddings) == 1

    asyncio.run(exercise())


def _tool_result(tool_name: str, data: dict) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        operation_id="op-1",
        status=ToolStatus.SUCCEEDED,
        data=data,
        error=None,
        observed_at="2026-04-01T00:00:00Z",
        duration_ms=1,
        source_ref="sandbox",
        truncated=False,
    )


def test_statement_gate_matches_completion_parts_for_checklist_and_other_predicates():
    """RM32, #295: incremental StatementGate enforces the exact safety rules of completion_parts."""
    health_result = [
        _tool_result(
            "get_service_health",
            {
                "service": "billing-api",
                "status": "degraded",
                "checked_at": "2026-04-01T00:00:00Z",
            },
        )
    ]

    # Contradicting status after Observation: heading is rejected even after multi-line checklist.
    gate = predicates.StatementGate("health_checklist_separated", health_result)
    out = gate.feed(
        "Observation: billing-api is degraded at 2026-04-01T00:00:00Z.\n"
        "Diagnostic checklist (draft, not log-backed)\n"
        "- Check if billing-api is healthy after restart.\n"
        "- Check upstream timeouts.\n"
    )
    assert "Diagnostic checklist" in out
    with pytest.raises(predicates.StatementRejected):
        gate.feed("Observation: billing-api is healthy.\n")

    # Same-line Observation: right after draft label resumes observation checks immediately.
    same_line = predicates.StatementGate("health_checklist_separated", health_result)
    with pytest.raises(predicates.StatementRejected):
        same_line.feed(
            "Diagnostic checklist (draft, not log-backed) Observation: billing-api is healthy.\n"
        )

    # Contradicting status in a later sentence is rejected for deployment/incident/deletion.
    dep_gate = predicates.StatementGate(
        "deployment_reported",
        [
            _tool_result(
                "get_latest_deployment",
                {
                    "service": "billing-api",
                    "deployment_id": "dep-101",
                    "deployed_at": "2026-04-01T00:00:00Z",
                },
            )
        ],
    )
    assert dep_gate.feed("Latest deployment for billing-api is dep-101. ")
    with pytest.raises(predicates.StatementRejected):
        dep_gate.feed("Previous deployment dep-999 failed. ")

    del_gate = predicates.StatementGate(
        "deletion_outcome_reported",
        [
            _tool_result(
                "delete_demo_database",
                {"database_id": "demo-alpha-db", "deleted": True},
            )
        ],
    )
    assert del_gate.feed("Deleted demo-alpha-db. ")
    with pytest.raises(predicates.StatementRejected):
        del_gate.feed("Actually it was not deleted. ")


def test_statement_gate_is_incremental_and_caches_id_mention(monkeypatch):
    """RM32, #295: StatementGate verifies each new statement without O(answer^2) rescanning."""
    assert predicates._id_mention("staging-api") is predicates._id_mention("staging-api")

    result = ToolResult(
        tool_name="get_service_health",
        operation_id="op-1",
        status=ToolStatus.SUCCEEDED,
        data={
            "service": "staging-api",
            "status": "healthy",
            "checked_at": "2026-10-20T02:58:00Z",
        },
        error=None,
        observed_at="2026-10-20T02:58:00Z",
        duration_ms=1,
        truncated=False,
        source_ref="sandbox://health/staging-api",
    )
    gate = predicates.StatementGate("health_status_preserved", [result])

    scanned_chars = 0
    real_parts = predicates.completion_parts

    def counted_parts(predicate_id, text, results):
        nonlocal scanned_chars
        scanned_chars += len(text)
        return real_parts(predicate_id, text, results)

    monkeypatch.setattr(predicates, "completion_parts", counted_parts)

    sentence = "staging-api is healthy (checked at 2026-10-20T02:58:00Z). "
    total_len = len(sentence) * 40
    emitted = []
    for _ in range(40):
        emitted.extend(gate.push(sentence))
    emitted.append(gate.finish())

    assert "".join(emitted) == sentence * 40
    # Linear incremental push + one EOF finish check stays well under 3x total_len
    # (quadratic rescanning over 40 sentences scans ~20x total_len).
    assert scanned_chars <= total_len * 3, (
        f"scanned {scanned_chars} chars for {total_len}-char text"
    )


def test_guidance_named_in_prompt_runs_off_thread_for_large_asset_lists(monkeypatch):
    """RM45, #295: _named_in_prompt runs off the event loop when offered assets exceed threshold."""
    assets = [
        {"name": f"skill-{i:04d}", "kind": "skill", "description": f"Tool number {i}"}
        for i in range(250)
    ]
    request = {
        "schema_version": 1,
        "task_text": "Please run skill-0123 for me now",
        "assets": assets,
    }
    settings = guidance_pipeline.GuidanceSettings(enabled=True)

    threads: list[int] = []
    real_named = guidance_pipeline._named_in_prompt

    def tracked_named(items, text):
        threads.append(threading.get_ident())
        return real_named(items, text)

    monkeypatch.setattr(guidance_pipeline, "_named_in_prompt", tracked_named)

    async def exercise():
        loop_thread = threading.get_ident()
        answer = await guidance_pipeline.run_guidance(request, settings)
        assert any(p["name"] == "skill-0123" for p in answer["picks"])
        assert threads and all(tid != loop_thread for tid in threads)

    asyncio.run(exercise())


def test_guidance_named_in_prompt_handles_trailing_newline_asset_name():
    """RM45, #295: simple-token fast path does not match asset names with trailing newlines."""
    assets = [{"name": "foo-bar\n", "kind": "skill", "description": "newline name"}]
    matched = guidance_pipeline._named_in_prompt(assets, "please use foo-bar\n now")
    assert len(matched) == 1 and matched[0]["name"] == "foo-bar\n"


def test_adaptation_stage_guard_fails_closed_when_older_record_expires(tmp_path):
    """RM42, #295: guard() fails closed when a record outside the top-20 snapshot expires."""
    from datetime import datetime

    from recommendation_helpers import BODY
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.selection.adaptation_stage import AdaptationFailure, AdaptationStage
    from test_asset_adaptation_stage import config as adaptation_config
    from test_asset_adaptation_stage import request_for
    from test_asset_adaptation_store import correct
    from test_asset_user_actions import action_client
    from test_embedding_backend import NativeDouble

    async def exercise():
        clock = FakeClock()
        async with action_client(tmp_path, clock=clock) as (
            client,
            app,
            service,
            runs,
            catalog,
            _,
        ):
            parent1 = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            # Record 1st correction on parent1 at t=0, then 20 newer corrections on parent2
            # 60s later so the top-20 snapshot excludes the oldest record and has a later expiry.
            await correct(client, parent1, 0, "older-0")
            clock.advance(60)
            parent = (
                await client.post(
                    "/relay/v1/recommendations",
                    json=BODY,
                    headers={"Idempotency-Key": "parent-2"},
                )
            ).json()
            for i in range(1, 21):
                clock.advance(1)
                await correct(client, parent, i, f"recent-{i}")

            semantic = adaptation_config(tmp_path)
            stage = AdaptationStage(
                app.state.feedback_store,
                semantic,
                EmbeddingEngine(semantic.manifest, NativeDouble()),
            )
            primary = parent["result"]["attempts"][0]["result"]
            req = request_for(service, catalog)
            outcome = await stage.apply(
                primary,
                req,
                budget=service.new_budget(),
                current_catalog=service.provider.current,
            )
            # Advance past the 21st (excluded) record's retain_until while top-20 stay valid.
            with runs.sql_read() as db:
                expiries = [
                    r[0]
                    for r in db.execute(
                        "SELECT retain_until FROM feedback_records ORDER BY retain_until"
                    ).fetchall()
                ]
            assert len(expiries) == 21 and expiries[0] < expiries[1]
            oldest_exp = datetime.fromisoformat(expiries[0].replace("Z", "+00:00"))
            # Move UTC time just past the 21st record's expiry while keeping monotonic budget alive.
            clock.now = oldest_exp + timedelta(seconds=1)
            with pytest.raises(AdaptationFailure):
                outcome.guard()
            outcome.close()
            stage.close()

    asyncio.run(exercise())
