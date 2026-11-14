"""Storage selection. Secrets live in environment variables, never app_config.json."""
import os


def database_url(config):
    if config.get("storage_backend", "sqlite") == "sqlite":
        return None
    if config.get("storage_backend") != "postgres":
        raise ValueError("Unknown storage_backend")
    value = os.environ.get(config.get("database_url_env", "WECHAT_BOT_DATABASE_URL"), "")
    if not value.startswith(("postgresql://", "postgres://")):
        raise ValueError("PostgreSQL selected: database URL environment variable is missing or invalid")
    return value


def create_memory(config):
    from .memory import MemoryManager
    from .postgres_memory import PostgresMemoryManager

    options = dict(
        ttl_sec=config.get("memory_ttl_sec"),
        cleanup_interval_sec=float(config.get("memory_cleanup_interval_sec", 0) or 0),
    )
    url = database_url(config)
    if url:
        return PostgresMemoryManager(url, **options)
    return MemoryManager(config.get("memory_db_path") or config.get("sqlite_db_path") or "data/chat_memory.db", **options)


def create_vectors(config):
    from .vector_memory import VectorMemory

    url = database_url(config)
    if url:
        from .postgres_vectors import PostgresVectorMemory
        return PostgresVectorMemory(url, dimensions=int(config.get("embedding_dimensions", 1536)))
    return VectorMemory()
