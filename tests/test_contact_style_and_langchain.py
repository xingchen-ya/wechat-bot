from types import SimpleNamespace

import pytest

from backend.core.memory import MemoryManager
from backend.core.rag import RetrievalService
from backend.core.vector_memory import VectorMemory


@pytest.mark.asyncio
async def test_style_is_persisted_and_does_not_replace_prompt_or_inferred_profile(tmp_path):
    memory = MemoryManager(str(tmp_path / "test.db"))
    await memory.update_user_profile("a", personality="inferred", preferences={"topics": ["coding"]})
    await memory.save_contact_prompt("a", "existing manual prompt", source="user_edit")
    await memory.save_contact_style("a", {"tone": "自然", "verbosity": "brief", "avoided_phrases": ["套话"]})
    await memory.update_user_profile("a", personality="new inference")
    await memory.close()
    memory = MemoryManager(str(tmp_path / "test.db"))
    snapshot = await memory.get_profile_prompt_snapshot("a")
    assert "自然" in snapshot["profile_summary"]
    assert snapshot["personality"] == "new inference"
    assert snapshot["contact_prompt"] == "existing manual prompt"
    assert snapshot["preferences"]["topics"] == ["coding"]
    assert not (await memory.get_contact_profile("b"))["preferences"]
    await memory.save_contact_style("a", None)
    assert "personal_style" not in (await memory.get_contact_profile("a"))["preferences"]
    with pytest.raises(ValueError):
        await memory.save_contact_style("a", {"verbosity": "invalid"})
    await memory.close()


@pytest.mark.asyncio
async def test_langchain_retrieval_keeps_contact_source_and_distances(tmp_path, monkeypatch):
    # A real local vector store and real LangChain retriever; no model or network.
    from backend.core.langchain_rag import retrieve
    import langsmith
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setattr(langsmith.Client, "create_run", lambda *args, **kwargs: pytest.fail("External tracing must be disabled"))
    store = VectorMemory(db_path=str(tmp_path))
    for ident, chat, source in [("a", "alice", "runtime_chat"), ("b", "bob", "runtime_chat"), ("c", "alice", "export_chat")]:
        store.add_text("coding style", {"chat_id": chat, "source": source}, ident, [1, 0])
    runtime = SimpleNamespace(bot_cfg={"rag_retriever_backend": "langchain"}, retriever_fetch_k=5)
    service = RetrievalService(runtime)
    hits = await service._search_vectors(store, "coding", filters={"chat_id": "alice", "source": "runtime_chat"}, query_embedding=[1, 0])
    assert [hit["id"] for hit in hits] == ["a"]
    assert hits[0]["distance"] == pytest.approx(0)
    with pytest.raises(ValueError):
        retrieve(store, "coding", filters={"source": "runtime_chat"}, k=5)
    store.conn.close()
