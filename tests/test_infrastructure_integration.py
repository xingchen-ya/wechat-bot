"""Real local-service tests; opt in with TEST_DATABASE_URL and TEST_REDIS_URL.

Only random test schemas / stream keys are removed. No real transport or model.
"""
import asyncio
import os
import time
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from backend.core.memory import MemoryManager
from backend.core.postgres_memory import PostgresMemoryManager
from backend.core.postgres_vectors import PostgresVectorMemory
from backend.core.scheduled_messages import ScheduledSender, StreamQueue, TaskStore
from backend.core.vector_memory import VectorMemory
from scripts.migrate_postgres import migrate


@pytest.fixture
def pg_url():
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is not set (local disposable PostgreSQL required)")
    assert urlsplit(base).hostname in {"localhost", "127.0.0.1"}, "Only local disposable services are allowed"
    import psycopg
    from psycopg import sql
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        url = psycopg.conninfo.make_conninfo(base, options=f"-csearch_path={schema},public")
        try:
            yield url
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.asyncio
async def test_postgres_memory_contract(pg_url):
    memory = PostgresMemoryManager(pg_url)
    try:
        await memory.initialize()
        assert await memory.add_messages("alice", [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]) == 2
        assert len(await memory.get_recent_context("alice")) == 2
        await memory.save_contact_style("alice", {"tone": "自然"})
        assert "自然" in (await memory.get_profile_prompt_snapshot("alice"))["profile_summary"]
        await memory.add_message("bob", "user", "other contact")
        assert len((await memory.get_recent_context_batch(["alice", "bob"]))["bob"]) == 1
        assert len(await memory.list_chat_summaries()) == 2
        await memory.upsert_background_backlog("alice", "profile", {"count": 1})
        assert (await memory.list_background_backlog())[0]["chat_id"] == "alice"
        draft = await memory.create_pending_reply(chat_id="alice", source_message_id="source", trigger_reason="manual", draft_reply="synthetic")
        assert draft["id"] > 0
        assert (await memory.get_pending_reply(draft["id"]))["draft_reply"] == "synthetic"
    finally:
        await memory.close()


def test_pgvector_scope_dimensions_and_upsert(pg_url):
    vectors = PostgresVectorMemory(pg_url, dimensions=2)
    try:
        for ident, chat in [("a", "alice"), ("b", "bob")]:
            vectors.add_text("hello coding", {"chat_id": chat, "source": "runtime_chat"}, ident, [1, 0])
        assert vectors.count() == 2
        hits = vectors.search(query_embedding=[1, 0], filter_meta={"chat_id": "alice", "source": "runtime_chat"})
        assert [hit["id"] for hit in hits] == ["a"]
        assert hits[0]["distance"] == pytest.approx(0)
        vectors.upsert_text("updated", {"chat_id": "alice"}, "a", [0, 1])
        assert vectors.count() == 2
        vectors.delete({"chat_id": "bob"})
        assert vectors.count() == 1
        with pytest.raises(ValueError):
            PostgresVectorMemory(pg_url, dimensions=3)
    finally:
        vectors.close()


def test_verified_migration_and_repeat_refusal(pg_url, tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    async def seed():
        manager = MemoryManager(str(path))
        await manager.add_message("synthetic", "user", "迁移校验")
        await manager.save_contact_style("synthetic", {"tone": "自然"})
        await manager.close()
    asyncio.run(seed())
    vectors = VectorMemory(str(tmp_path / "vectors"))
    vectors.add_text("sample", {"chat_id": "synthetic", "source": "runtime_chat"}, "id", [0.1, 0.9])
    vector_path = vectors._db_file
    vectors.conn.close()
    original = path.read_bytes()
    monkeypatch.setenv("WECHAT_BOT_DATABASE_URL", pg_url)
    report = migrate(path, vector_path, apply=True, dimensions=2)
    assert report["source_counts"]["chat_history"] == 1
    assert len(report["verified_checksums"]) == 5
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="not empty"):
        migrate(path, vector_path, apply=True, dimensions=2)


@pytest.mark.asyncio
async def test_real_stream_task_restart_duplicate_cancel_expiry(pg_url, monkeypatch):
    redis_url = os.environ.get("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL is not set")
    assert urlsplit(redis_url).hostname in {"localhost", "127.0.0.1"}
    import backend.core.scheduled_messages as module
    stream = "test:scheduled:" + uuid.uuid4().hex
    monkeypatch.setattr(module, "STREAM", stream)
    store = TaskStore(pg_url)
    queue = StreamQueue(redis_url)
    send = AsyncMock(return_value={"success": True})
    config = {"scheduled_messages_enabled": True, "scheduled_message_targets": ["synthetic"]}
    worker = ScheduledSender(store, queue, send, lambda: config)
    try:
        await queue.initialize()
        ident = store.add(target="synthetic", content="no real message", due_at=int(time.time()) + 10, allowed_targets=["synthetic"])
        # Simulate process restart before a task becomes due.
        store.close()
        store = TaskStore(pg_url)
        worker.store = store
        store.conn.execute("UPDATE scheduled_messages SET due_at=%s WHERE id=%s", (int(time.time()) - 1, ident))
        await queue.publish(ident)  # duplicate dual-write delivery
        await worker.tick()
        assert store.list()[0]["state"] == "sent"
        send.assert_awaited_once()
        await queue.publish(ident)
        await worker.tick()
        send.assert_awaited_once()
        cancelled = store.add(target="synthetic", content="cancel", due_at=int(time.time()) + 10, allowed_targets=["synthetic"])
        assert store.cancel(cancelled)
        expired = store.add(target="synthetic", content="expire", due_at=int(time.time()) + 10, allowed_targets=["synthetic"])
        store.conn.execute("UPDATE scheduled_messages SET due_at=0,expires_at=1 WHERE id=%s", (expired,))
        uncertain = store.add(target="synthetic", content="uncertain", due_at=int(time.time()) + 10, allowed_targets=["synthetic"])
        store.conn.execute("UPDATE scheduled_messages SET state='sending',updated_at=0 WHERE id=%s", (uncertain,))
        await worker.tick()
        states = {task["id"]: task["state"] for task in store.list()}
        assert states[cancelled] == "cancelled"
        assert states[expired] == "expired"
        assert states[uncertain] == "uncertain"
        send.assert_awaited_once()
        # Broken PostgreSQL connection recovers without replaying any external send.
        broken_connection = store.conn
        broken_connection.close()
        runner = asyncio.create_task(worker.run())
        try:
            for _ in range(100):
                if store.conn is not broken_connection and not store.conn.closed:
                    break
                await asyncio.sleep(0.05)
            assert store.conn is not broken_connection
            assert not store.conn.closed
        finally:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        send.assert_awaited_once()
    finally:
        await queue.redis.delete(stream)
        await worker.close()


def test_migration_rolls_back_prior_tables_on_late_schema_failure(pg_url, tmp_path, monkeypatch):
    import sqlite3
    import psycopg
    path = tmp_path / "memory.db"
    async def seed():
        memory = MemoryManager(str(path))
        await memory.add_message("synthetic", "user", "must roll back")
        await memory.close()
    asyncio.run(seed())
    with sqlite3.connect(path) as source:
        source.execute("ALTER TABLE user_profiles ADD COLUMN custom_secret TEXT")
    original = path.read_bytes()
    monkeypatch.setenv("WECHAT_BOT_DATABASE_URL", pg_url)
    with pytest.raises(ValueError, match="Unknown memory columns"):
        migrate(path, apply=True, dimensions=2)
    with psycopg.connect(pg_url) as target:
        assert target.execute("SELECT count(*) FROM chat_history").fetchone()[0] == 0
    assert path.read_bytes() == original
