from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import backend.api as api
from backend.core.memory import MemoryManager


@pytest.mark.asyncio
async def test_contact_style_api_persistence_validation_and_clear(tmp_path, monkeypatch):
    memory = MemoryManager(str(tmp_path / "memory.db"))
    manager = Mock()
    manager.get_memory_manager.return_value = memory
    monkeypatch.setattr(api, "manager", manager)
    api.app.config["TESTING"] = True
    client = api.app.test_client()
    try:
        response = await client.post("/api/contact_style", json={"chat_id": "synthetic", "style": {"tone": "自然"}})
        assert response.status_code == 200
        assert (await response.get_json())["profile"]["preferences"]["personal_style"]["tone"] == "自然"
        assert (await client.post("/api/contact_style", json={"chat_id": "synthetic", "style": {"unknown": 1}})).status_code == 400
        assert (await client.post("/api/contact_style", json=[])).status_code == 400
        assert (await client.post("/api/contact_style", json={"chat_id": "synthetic", "style": None})).status_code == 200
    finally:
        await memory.close()


@pytest.mark.asyncio
async def test_task_api_disabled_and_service_failure_are_safe(monkeypatch):
    manager = Mock()
    manager.config_service.get_snapshot.return_value = SimpleNamespace(bot={})
    monkeypatch.setattr(api, "manager", manager)
    client = api.app.test_client()
    response = await client.post("/api/scheduled_messages", json={"target": "synthetic", "content": "never sent", "due_at": 9999999999})
    assert response.status_code == 409
    manager.config_service.get_snapshot.return_value = SimpleNamespace(bot={"storage_backend": "postgres"})
    import backend.core.scheduled_messages as module
    monkeypatch.setattr(module, "task_store", Mock(side_effect=RuntimeError("secret DSN must never appear")))
    response = await client.get("/api/scheduled_messages")
    assert response.status_code == 503
    assert "secret DSN" not in (await response.get_data()).decode()


@pytest.mark.asyncio
async def test_task_api_only_persists_and_cancels(monkeypatch):
    manager = Mock()
    manager.send_text_message = AsyncMock()
    manager.config_service.get_snapshot.return_value = SimpleNamespace(bot={"scheduled_messages_enabled": True, "scheduled_message_targets": ["synthetic"]})
    monkeypatch.setattr(api, "manager", manager)
    import backend.core.scheduled_messages as module
    store = Mock()
    store.add.return_value = "task-id"
    store.cancel.return_value = True
    monkeypatch.setattr(module, "task_store", Mock(return_value=store))
    client = api.app.test_client()
    response = await client.post("/api/scheduled_messages", json={"target": "synthetic", "content": "offline", "due_at": 9999999999})
    assert (await response.get_json())["id"] == "task-id"
    assert (await client.delete("/api/scheduled_messages", json={"id": "task-id"})).status_code == 200
    manager.send_text_message.assert_not_awaited()
    assert store.close.call_count == 2
