"""Optional LangChain retrieval boundary; never owns model invocation or sending."""
from typing import Any, Optional

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langsmith import tracing_context


class ScopedMemoryRetriever(BaseRetriever):
    store: Any
    filters: dict
    k: int = 5
    query_embedding: Optional[list[float]] = None

    def _get_relevant_documents(self, query, *, run_manager):
        hits = self.store.search(
            query=query if not self.query_embedding else None,
            query_embedding=self.query_embedding,
            filter_meta=dict(self.filters),
            n_results=self.k,
        )
        return [Document(page_content=str(hit.get("text") or ""), metadata={"hit": hit}) for hit in hits]


def retrieve(store, query, *, filters, k, query_embedding=None):
    if not filters.get("source"):
        raise ValueError("Retrieval must be scoped to a source")
    if filters["source"] == "runtime_chat" and not filters.get("chat_id"):
        raise ValueError("Runtime retrieval requires a contact scope")
    retriever = ScopedMemoryRetriever(store=store, filters=filters, k=k, query_embedding=query_embedding)
    # No LangSmith export, even if the host process has tracing environment flags.
    with tracing_context(enabled=False):
        documents = retriever.invoke(query, config={"callbacks": []})
    return [dict(doc.metadata["hit"]) for doc in documents]
