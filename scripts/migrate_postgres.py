"""Offline, read-only-source migration. Default is a source-only dry run.

Failure modes: unknown source columns could be lost; a nonempty target could be
overwritten; concurrent writers could invalidate verification. Refuse the first
two and require stopped writers. All imports and verification share one target
transaction; original SQLite files are never changed or removed.
"""
import argparse
import asyncio
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MEMORY_TABLES = ("chat_history", "user_profiles", "background_backlog", "pending_replies")


def open_source(path):
    source = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")  # consistent snapshot, including committed WAL rows
    return source


def source_tables(source, allowed):
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'") if not row[0].startswith("sqlite_")}
    if tables - set(allowed):
        raise ValueError("Unknown source tables; migration aborted without dropping data")
    return sorted(tables)


def update_checksum(checksum, rows):
    for row in rows:
        checksum.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode())
        checksum.update(b"\n")


def vector_values(row, dimensions):
    from backend.core.postgres_vectors import serialize_embedding
    meta = json.loads(row["metadata"])
    for key in ("source", "chat_id", "doc_id"):
        if key in row.keys() and row[key] is not None:
            if key in meta and meta[key] != row[key]:
                raise ValueError("Conflicting vector metadata; resolve the source before migration")
            meta[key] = row[key]
    embedding = serialize_embedding(json.loads(row["embedding"]), dimensions) if row["embedding"] else None
    return (row["id"], row["text"], json.dumps(meta, ensure_ascii=False, sort_keys=True), embedding)


def canonical_vectors(items):
    # pgvector stores float32; compare at that precision, without losing JSON fields.
    for ident, text, meta, emb in items:
        meta = json.loads(meta) if isinstance(meta, str) else meta
        emb = json.loads(emb) if isinstance(emb, str) else emb
        yield (ident, text, json.dumps(meta, ensure_ascii=False, sort_keys=True),
               [struct.unpack("f", struct.pack("f", float(x)))[0] for x in emb] if emb else None)


def migrate(memory_path, vector_path=None, *, apply=False, dimensions=1536):
    from backend.core.postgres_vectors import PostgresVectorMemory
    from backend.core.postgres_memory import PostgresMemoryManager
    from psycopg import sql
    import psycopg

    with ExitStack() as stack:
        sources = [(stack.enter_context(closing(open_source(memory_path))), MEMORY_TABLES)]
        if vector_path:
            sources.append((stack.enter_context(closing(open_source(vector_path))), ("vectors",)))
        inventories = [(source, source_tables(source, allowed)) for source, allowed in sources]
        counts = {table: source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                  for source, tables in inventories for table in tables}
        if not apply:
            return {"dry_run": True, "source_counts": counts, "target_checked": False}
        url = os.environ.get("WECHAT_BOT_DATABASE_URL", "")
        if not url:
            raise ValueError("WECHAT_BOT_DATABASE_URL is required for --apply")

        async def initialize():
            manager = PostgresMemoryManager(url)
            try:
                await manager.initialize()
            finally:
                await manager.close()
        asyncio.run(initialize())
        vectors = PostgresVectorMemory(url, dimensions)
        vectors.close()
        with psycopg.connect(url) as target:
            # Include all tables, even absent source tables. Refuse mixing datasets.
            for table in (*MEMORY_TABLES, "vectors"):
                target.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table)))
                if target.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0]:
                    raise ValueError("Target is not empty; use a new database (never truncate)")
            checksums = {}
            for source, tables in inventories:
                for table in tables:
                    columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                    target_columns = {row[0] for row in target.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=%s", (table,))}
                    if table == "vectors":
                        # Denormalized SQLite filter columns are preserved inside metadata.
                        allowed = {"id", "text", "metadata", "embedding", "source", "chat_id", "doc_id"}
                        if set(columns) - allowed:
                            raise ValueError("Unknown vector columns; refusing lossy import")
                        columns = ["id", "text", "metadata", "embedding"]
                    elif set(columns) - target_columns:
                        raise ValueError("Unknown memory columns; refusing lossy import")
                    order = "id" if "id" in columns else "wx_id" if "wx_id" in columns else "chat_id,task_type"
                    rows = source.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
                    names = sql.SQL(",").join(map(sql.Identifier, columns))
                    placeholders = sql.SQL(",").join(sql.SQL("%s::jsonb" if c == "metadata" and table == "vectors" else "%s::vector" if c == "embedding" and table == "vectors" else "%s") for c in columns)
                    expected, actual = hashlib.sha256(), hashlib.sha256()
                    source_count, target_count = 0, 0
                    with target.cursor() as cursor:
                        while batch := rows.fetchmany(500):
                            values = [vector_values(row, dimensions) if table == "vectors" else tuple(row[c] for c in columns) for row in batch]
                            cursor.executemany(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(table), names, placeholders), values)
                            update_checksum(expected, canonical_vectors(values) if table == "vectors" else values)
                            source_count += len(values)
                    with target.cursor(name="verify_" + table) as imported:
                        imported.execute(sql.SQL("SELECT {} FROM {} ORDER BY " + order).format(names, sql.Identifier(table)))
                        while batch := imported.fetchmany(500):
                            update_checksum(actual, canonical_vectors(batch) if table == "vectors" else batch)
                            target_count += len(batch)
                    if target_count != source_count or expected.digest() != actual.digest():
                        raise ValueError("Target verification failed; import transaction rolled back")
                    checksums[table] = actual.hexdigest()
            for table in ("chat_history", "pending_replies"):
                target.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {}").format(sql.Identifier(table)), (table,))
        return {"dry_run": False, "source_counts": counts, "verified_checksums": checksums}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", required=True)
    parser.add_argument("--vectors")
    parser.add_argument("--dimensions", type=int, default=1536)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.memory, args.vectors, apply=args.apply, dimensions=args.dimensions)))
    except Exception as exc:
        # Driver errors can contain DSNs, hostnames or source data. Do not print them.
        print(json.dumps({"success": False, "error_type": type(exc).__name__, "message": "Migration refused or failed. Sources retained; inspect configuration/schema privately."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
