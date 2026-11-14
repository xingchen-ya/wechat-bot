"""Persistent UTC one-shot tasks + Redis Streams outbox.

Inspired by timetask's durable task/poll/dispatch pattern, not an imported plugin.
Failure modes: DB/Redis dual-write duplication, crash after external send, and
stale tasks after downtime. Task IDs deduplicate claims; uncertain sends never
auto-retry; expiry bounds catch-up. Exactly-once external delivery is impossible
without a transport idempotency receipt.
"""
import asyncio
import logging
import os
import time
import uuid

from .storage import database_url

logger = logging.getLogger(__name__)
STREAM = "wechat-bot:scheduled:v1"
GROUP = "desktop-senders"
LEASE_SECONDS = 300


class TaskStore:
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self._url = url
        self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row, connect_timeout=10)
        try:
            self._initialize()
        except BaseException:
            self.conn.close()
            raise

    def _initialize(self):
        self.conn.execute("""CREATE TABLE IF NOT EXISTS scheduled_messages (
            id TEXT PRIMARY KEY, target TEXT NOT NULL, content TEXT NOT NULL,
            due_at BIGINT NOT NULL, expires_at BIGINT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','queued','sending','sent','cancelled','expired','uncertain')),
            updated_at BIGINT NOT NULL, worker TEXT NOT NULL DEFAULT '')""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled_messages(state,due_at)")

    def add(self, *, target, content, due_at, allowed_targets, task_id=None, max_lateness=3600):
        if target not in allowed_targets or not content.strip() or len(content) > 4000:
            raise ValueError("Target must be allowlisted and content must contain 1..4000 characters")
        if isinstance(due_at, bool) or not isinstance(due_at, int) or due_at <= int(time.time()):
            raise ValueError("due_at must be a future UTC Unix timestamp in seconds")
        task_id = str(uuid.UUID(task_id)) if task_id else str(uuid.uuid4())
        # Same caller-supplied ID cannot silently replace an existing task.
        self.conn.execute("""INSERT INTO scheduled_messages(id,target,content,due_at,expires_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""", (task_id, target, content, due_at, due_at + max_lateness, int(time.time())))
        existing = self.conn.execute("SELECT target,content,due_at FROM scheduled_messages WHERE id=%s", (task_id,)).fetchone()
        if (existing["target"], existing["content"], existing["due_at"]) != (target, content, due_at):
            raise ValueError("Task ID already exists with different contents")
        return task_id

    def list(self):
        # Do not return message contents or contact identities in operational lists.
        return self.conn.execute("SELECT id,due_at,expires_at,state,updated_at FROM scheduled_messages ORDER BY due_at DESC LIMIT 200").fetchall()

    def cancel(self, task_id):
        return self.conn.execute("""UPDATE scheduled_messages SET state='cancelled',updated_at=%s
            WHERE id=%s AND state IN ('pending','queued')""", (int(time.time()), task_id)).rowcount == 1

    def due(self, now):
        self.conn.execute("""UPDATE scheduled_messages SET state='expired',updated_at=%s
            WHERE state IN ('pending','queued') AND expires_at < %s""", (now, now))
        self.conn.execute("""UPDATE scheduled_messages SET state='uncertain',updated_at=%s
            WHERE state='sending' AND updated_at < %s""", (now, now - LEASE_SECONDS))
        # Re-enqueue queued tasks after Redis loss. Duplicate entries are harmless:
        # the PostgreSQL compare-and-set below owns the right to send.
        return self.conn.execute("""SELECT id FROM scheduled_messages WHERE
            (state='pending' OR (state='queued' AND updated_at < %s))
            AND due_at <= %s AND expires_at >= %s ORDER BY due_at LIMIT 50""", (now - LEASE_SECONDS, now, now)).fetchall()

    def queued(self, task_id, now):
        self.conn.execute("UPDATE scheduled_messages SET state='queued',updated_at=%s WHERE id=%s AND state IN ('pending','queued')", (now, task_id))

    def claim(self, task_id, worker, allowed_targets, now):
        if not allowed_targets:
            return None
        return self.conn.execute("""UPDATE scheduled_messages SET state='sending',worker=%s,updated_at=%s
            WHERE id=%s AND state IN ('pending','queued') AND due_at <= %s AND expires_at >= %s
            AND target = ANY(%s) RETURNING target,content""", (worker, now, task_id, now, now, list(allowed_targets))).fetchone()

    def finish(self, task_id, worker, state):
        if state not in {"sent", "uncertain"}:
            raise ValueError("Unsupported completion state")
        self.conn.execute("""UPDATE scheduled_messages SET state=%s,updated_at=%s
            WHERE id=%s AND worker=%s AND state='sending'""", (state, int(time.time()), task_id, worker))

    def close(self):
        self.conn.close()

    def reconnect(self):
        self.close()
        self.__init__(self._url)


class StreamQueue:
    def __init__(self, url):
        from redis.asyncio import Redis
        self.redis = Redis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        self.consumer = str(uuid.uuid4())
        self._claim_cursor = "0-0"

    async def initialize(self):
        from redis.exceptions import ResponseError
        try:
            await self.redis.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, task_id):
        # No approximate trimming: never drop unacknowledged entries.
        return await self.redis.xadd(STREAM, {"task_id": task_id})

    async def read(self):
        claimed = await self.redis.xautoclaim(STREAM, GROUP, self.consumer, LEASE_SECONDS * 1000, self._claim_cursor, count=10)
        self._claim_cursor = claimed[0]
        if claimed[1]:
            return claimed[1]
        entries = await self.redis.xreadgroup(GROUP, self.consumer, {STREAM: ">"}, count=10, block=100)
        return entries[0][1] if entries else []

    async def ack(self, message_id):
        # One group only. Delete only after the durable task state has settled.
        await self.redis.xack(STREAM, GROUP, message_id)
        await self.redis.xdel(STREAM, message_id)

    async def close(self):
        await self.redis.aclose()


class ScheduledSender:
    def __init__(self, store, queue, send, config):
        self.store, self.queue, self.send, self.config = store, queue, send, config
        self.worker = str(uuid.uuid4())

    async def tick(self):
        config = self.config()
        if not config.get("scheduled_messages_enabled", False):
            return
        now = int(time.time())
        for row in await asyncio.to_thread(self.store.due, now):
            await self.queue.publish(row["id"])
            await asyncio.to_thread(self.store.queued, row["id"], now)
        for message_id, fields in await self.queue.read():
            task_id = fields.get("task_id", "")
            config = self.config()  # disabling stops subsequent claims immediately
            if not config.get("scheduled_messages_enabled", False):
                return
            payload = await asyncio.to_thread(self.store.claim, task_id, self.worker, config.get("scheduled_message_targets", []), int(time.time()))
            if payload is not None:
                try:
                    result = await self.send(payload["target"], payload["content"])
                except asyncio.CancelledError:
                    # Leave 'sending'; recovery marks it uncertain, never resends.
                    raise
                except Exception:
                    result = {"success": False}
                state = "sent" if isinstance(result, dict) and result.get("success") is True else "uncertain"
                await asyncio.to_thread(self.store.finish, task_id, self.worker, state)
            await self.queue.ack(message_id)

    async def run(self):
        initialized = False
        while True:
            try:
                if not initialized:
                    await self.queue.initialize()
                    initialized = True
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Redis may have lost the group. Reinitialize on error, not every tick.
                initialized = False
                logger.warning("Scheduled sender paused this tick (%s)", type(exc).__name__)
                from psycopg import InterfaceError, OperationalError
                if isinstance(exc, (OperationalError, InterfaceError)):
                    try:
                        await asyncio.to_thread(self.store.reconnect)
                    except Exception as reconnect_error:
                        logger.warning("Scheduled database reconnect deferred (%s)", type(reconnect_error).__name__)
            await asyncio.sleep(1)

    async def close(self):
        await self.queue.close()
        await asyncio.to_thread(self.store.close)


def task_store(config):
    url = database_url(config)
    if not url:
        raise ValueError("Persistent scheduled sending requires storage_backend=postgres")
    return TaskStore(url)


async def create_sender(config, send):
    snapshot = config()
    url = os.environ.get(snapshot.get("redis_url_env", "WECHAT_BOT_REDIS_URL"), "")
    if not url.startswith(("redis://", "rediss://")):
        raise ValueError("Redis URL environment variable is missing or invalid")
    queue = StreamQueue(url)
    initialization = asyncio.create_task(asyncio.to_thread(task_store, snapshot))
    try:
        store = await asyncio.shield(initialization)
    except asyncio.CancelledError:
        # to_thread cannot stop a blocking driver connect. Retain its result and
        # dispose it even when stop() cancels startup before the connect returns.
        def dispose_late_store(completed):
            try:
                completed.result().close()
            except Exception as exc:
                logger.warning("Scheduled startup cleanup (%s)", type(exc).__name__)
        initialization.add_done_callback(dispose_late_store)
        await queue.close()
        raise
    except Exception:
        await queue.close()
        raise RuntimeError("Scheduled database initialization failed; check infrastructure privately") from None
    return ScheduledSender(store, queue, send, config)
