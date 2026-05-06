"""Reminders sync via Redis pub/sub — replication between failover nodes."""

import asyncio
import json
import logging
import sqlite3
from typing import Optional

import redis.asyncio as aioredis

from reminders import ReminderDB, get_db, utc_iso, parse_iso

logger = logging.getLogger("kesha.reminders_sync")

CHANNEL = "kesha:reminders:events"
DUMP_KEY = "kesha:reminders:dump"


class ReminderSync:
    def __init__(self, redis_url: str, node_id: str):
        self._redis_url = redis_url
        self._node_id = node_id
        self._redis: Optional[aioredis.Redis] = None
        self._sub_task: Optional[asyncio.Task] = None

    async def connect(self):
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        logger.info("ReminderSync: Redis connected")

    async def start_subscriber(self):
        self._sub_task = asyncio.create_task(self._subscribe_loop())

    async def stop(self):
        if self._sub_task and not self._sub_task.done():
            self._sub_task.cancel()
            try:
                await self._sub_task
            except asyncio.CancelledError:
                pass

    async def publish_event(self, action: str, data: dict):
        if not self._redis:
            return
        event = json.dumps({"node": self._node_id, "action": action, "data": data})
        try:
            await self._redis.publish(CHANNEL, event)
        except Exception as e:
            logger.debug(f"publish_event failed: {e}")

    async def push_dump(self):
        if not self._redis:
            return
        db = get_db()
        rows = db.conn.execute("SELECT * FROM reminders").fetchall()
        dump = [dict(r) for r in rows]
        await self._redis.set(DUMP_KEY, json.dumps(dump))
        logger.info(f"ReminderSync: pushed dump ({len(dump)} reminders)")

    async def pull_dump(self):
        if not self._redis:
            return
        raw = await self._redis.get(DUMP_KEY)
        if not raw:
            logger.info("ReminderSync: no dump in Redis, skipping")
            return
        rows = json.loads(raw)
        if not rows:
            return
        db = get_db()
        db.conn.execute("DELETE FROM reminders")
        for r in rows:
            cols = [k for k in r.keys() if k != "id"]
            vals = [r[k] for k in cols]
            placeholders = ",".join("?" * len(cols))
            col_names = ",".join(cols)
            db.conn.execute(
                f"INSERT INTO reminders(id, {col_names}) VALUES(?, {placeholders})",
                (r["id"], *vals),
            )
        logger.info(f"ReminderSync: pulled dump ({len(rows)} reminders)")

    async def _subscribe_loop(self):
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(CHANNEL)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                    if event.get("node") == self._node_id:
                        continue
                    await self._apply_event(event)
                except Exception as e:
                    logger.debug(f"ReminderSync event error: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"ReminderSync subscriber died: {e}")

    async def _apply_event(self, event: dict):
        action = event["action"]
        data = event["data"]
        db = get_db()

        if action == "create":
            existing = db.get(data["id"])
            if existing:
                return
            cols = [k for k in data.keys() if k != "id"]
            vals = [data[k] for k in cols]
            placeholders = ",".join("?" * len(cols))
            col_names = ",".join(cols)
            try:
                db.conn.execute(
                    f"INSERT INTO reminders(id, {col_names}) VALUES(?, {placeholders})",
                    (data["id"], *vals),
                )
                logger.info(f"ReminderSync: replicated create #{data['id']}")
            except sqlite3.IntegrityError:
                pass

        elif action == "cancel":
            db.cancel(data["id"])
            logger.info(f"ReminderSync: replicated cancel #{data['id']}")

        elif action == "update":
            rid = data.pop("id")
            if data:
                db.conn.execute(
                    f"UPDATE reminders SET {','.join(f'{k}=?' for k in data)} WHERE id=?",
                    (*data.values(), rid),
                )
                logger.info(f"ReminderSync: replicated update #{rid}")

        elif action == "mark_fired":
            db.conn.execute(
                "UPDATE reminders SET fired_at=?, delivered=? WHERE id=?",
                (data["fired_at"], data["delivered"], data["id"]),
            )

        elif action == "reschedule":
            db.conn.execute(
                "UPDATE reminders SET due_at=?, fired_at=NULL, delivered=0 WHERE id=?",
                (data["due_at"], data["id"]),
            )

        elif action == "mark_delivered_batch":
            ids = data["ids"]
            if ids:
                qs = ",".join("?" * len(ids))
                db.conn.execute(f"UPDATE reminders SET delivered=1 WHERE id IN ({qs})", tuple(ids))


_sync: Optional[ReminderSync] = None


def get_sync() -> Optional[ReminderSync]:
    return _sync


async def init_sync(redis_url: str, node_id: str) -> ReminderSync:
    global _sync
    _sync = ReminderSync(redis_url, node_id)
    await _sync.connect()
    await _sync.start_subscriber()
    return _sync
