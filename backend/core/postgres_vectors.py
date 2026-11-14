"""pgvector implementation of the existing synchronous VectorMemory contract."""
import json
import math
import threading

from .vector_memory import _rank_keyword_candidates


def serialize_embedding(values, dimensions):
    if values is None:
        return None
    values = [float(x) for x in values]
    if len(values) != dimensions or not all(math.isfinite(x) for x in values) or not any(values):
        raise ValueError("Embedding dimension, finiteness or norm is invalid")
    return json.dumps(values)


class PostgresVectorMemory:
    def __init__(self, url, dimensions=1536):
        import psycopg
        from psycopg.rows import dict_row

        if not 1 <= dimensions <= 16000:
            raise ValueError("embedding_dimensions must be between 1 and 16000")
        self.dimensions = dimensions
        self._lock = threading.RLock()
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        try:
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self.conn.execute(f"""CREATE TABLE IF NOT EXISTS vectors (
                id TEXT PRIMARY KEY, text TEXT NOT NULL, metadata JSONB NOT NULL,
                embedding vector({dimensions}))""")
            actual = self.conn.execute("""SELECT format_type(atttypid, atttypmod) AS type
                FROM pg_attribute WHERE attrelid = 'vectors'::regclass AND attname = 'embedding'""").fetchone()
            if actual["type"] != f"vector({dimensions})":
                raise ValueError("Existing vector dimension differs; use a new database and re-embed explicitly")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_metadata ON vectors USING gin(metadata)")
        except BaseException:
            self.conn.close()
            raise

    def __bool__(self):
        return not self.conn.closed

    def _embedding(self, values):
        return serialize_embedding(values, self.dimensions)

    def add_text(self, text, metadata, id, embedding=None):
        values = (str(id), str(text), json.dumps(metadata or {}, ensure_ascii=False), self._embedding(embedding))
        with self._lock:
            self.conn.execute("""INSERT INTO vectors (id,text,metadata,embedding) VALUES (%s,%s,%s::jsonb,%s::vector)
                ON CONFLICT(id) DO UPDATE SET text=excluded.text, metadata=excluded.metadata, embedding=excluded.embedding""", values)

    upsert_text = add_text

    def search(self, query=None, n_results=5, filter_meta=None, query_embedding=None):
        if not query_embedding:
            return self.keyword_search(query or "", n_results, filter_meta)
        vector = self._embedding(query_embedding)
        with self._lock:
            return self.conn.execute("""SELECT id,text,metadata,embedding <=> %s::vector AS distance
                FROM vectors WHERE metadata @> %s::jsonb AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector, id LIMIT %s""",
                (vector, json.dumps(filter_meta or {}), vector, max(1, int(n_results)))).fetchall()

    def keyword_search(self, query, n_results=5, filter_meta=None, candidate_limit=200):
        with self._lock:
            rows = self.conn.execute("""SELECT id,text,metadata FROM vectors WHERE metadata @> %s::jsonb
                ORDER BY id LIMIT %s""", (json.dumps(filter_meta or {}), min(1000, max(n_results, candidate_limit)))).fetchall()
        return _rank_keyword_candidates(query, rows)[:max(1, n_results)]

    def list_metadata(self, where=None, limit=1000):
        with self._lock:
            return self.conn.execute("SELECT id,metadata FROM vectors WHERE metadata @> %s::jsonb ORDER BY id LIMIT %s",
                (json.dumps(where or {}), min(5000, max(1, limit)))).fetchall()

    def delete(self, where):
        if not where:
            raise ValueError("Refusing unfiltered vector deletion")
        with self._lock:
            self.conn.execute("DELETE FROM vectors WHERE metadata @> %s::jsonb", (json.dumps(where),))

    def count(self, where=None):
        with self._lock:
            return self.conn.execute("SELECT count(*) AS total FROM vectors WHERE metadata @> %s::jsonb", (json.dumps(where or {}),)).fetchone()["total"]

    def close(self):
        with self._lock:
            self.conn.close()
