from unittest.mock import AsyncMock, Mock
import asyncio
import threading

import pytest

from backend.core.scheduled_messages import ScheduledSender, TaskStore


def sender(config=None):
    config = config or {"scheduled_messages_enabled": True, "scheduled_message_targets": ["synthetic"]}
    store = Mock()
    store.due.return_value = [{"id": "task"}]
    store.claim.return_value = {"target": "synthetic", "content": "offline"}
    queue = AsyncMock()
    queue.read.return_value = [("1-0", {"task_id": "task"})]
    send = AsyncMock(return_value={"success": True})
    return ScheduledSender(store, queue, send, lambda: config)


@pytest.mark.asyncio
async def test_publish_before_mark_and_ack_after_persist():
    worker = sender()
    events = []
    worker.queue.publish.side_effect = lambda *args: events.append("publish")
    worker.store.queued.side_effect = lambda *args: events.append("queued")
    worker.send.side_effect = lambda *args: events.append("send") or {"success": True}
    worker.store.finish.side_effect = lambda *args: events.append("finish")
    worker.queue.ack.side_effect = lambda *args: events.append("ack")
    await worker.tick()
    assert events == ["publish", "queued", "send", "finish", "ack"]


@pytest.mark.asyncio
async def test_duplicate_claim_does_not_send():
    worker = sender()
    worker.store.claim.return_value = None
    await worker.tick()
    worker.send.assert_not_awaited()
    worker.queue.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncertain_send_is_persisted_before_ack():
    worker = sender()
    worker.send.side_effect = TimeoutError("synthetic")
    await worker.tick()
    assert worker.store.finish.call_args.args[-1] == "uncertain"
    worker.queue.ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_failure_after_send_leaves_entry_unacked():
    worker = sender()
    worker.store.finish.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError):
        await worker.tick()
    worker.queue.ack.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_failure_leaves_task_pending():
    worker = sender()
    worker.queue.publish.side_effect = ConnectionError()
    with pytest.raises(ConnectionError):
        await worker.tick()
    worker.store.queued.assert_not_called()
    worker.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_worker_never_reads_or_sends():
    worker = sender({"scheduled_messages_enabled": False})
    await worker.tick()
    worker.store.due.assert_not_called()
    worker.send.assert_not_awaited()


def test_task_validation_precedes_any_write():
    store = object.__new__(TaskStore)
    store.conn = Mock()
    for options in [dict(target="other", content="x", due_at=9999999999), dict(target="synthetic", content="", due_at=9999999999), dict(target="synthetic", content="x", due_at=0)]:
        with pytest.raises(ValueError):
            store.add(**options, allowed_targets=["synthetic"])
    store.conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_startup_closes_late_database_and_preserves_cancellation(monkeypatch):
    import backend.core.scheduled_messages as module
    entered, release = threading.Event(), threading.Event()
    store, queue = Mock(), AsyncMock()
    def connect(config):
        entered.set()
        release.wait(5)
        return store
    monkeypatch.setenv("WECHAT_BOT_REDIS_URL", "redis://127.0.0.1/15")
    monkeypatch.setattr(module, "StreamQueue", Mock(return_value=queue))
    monkeypatch.setattr(module, "task_store", connect)
    task = asyncio.create_task(module.create_sender(lambda: {}, AsyncMock()))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        queue.close.assert_awaited_once()
    finally:
        release.set()
    for _ in range(100):
        if store.close.called:
            break
        await asyncio.sleep(0.01)
    store.close.assert_called_once()
