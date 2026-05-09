"""Failover v2 — simple master/slave. VPS = data server, Laptop = client."""

import asyncio
import time
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from config import logger

HEARTBEAT_KEY = "kesha:heartbeat"
SESSION_KEY_PREFIX = "kesha:sessions:"
REMINDERS_DUMP_KEY = "kesha:reminders:dump"

HEARTBEAT_INTERVAL = 15
HEARTBEAT_TIMEOUT = 60


class FailoverNode:
    """Runs on both nodes. Laptop sends heartbeats, VPS watches them."""

    def __init__(self, node_id: str, redis_url: str):
        self.node_id = node_id
        self.is_laptop = node_id == "laptop"
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None
        self._active = False
        self._polling_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._reminder_stop = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def connect(self):
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        logger.info("Failover: Redis connected (%s)", self._redis_url)

    async def get_session(self, chat_id: int) -> str | None:
        try:
            return await self._redis.get(f"{SESSION_KEY_PREFIX}{chat_id}")
        except Exception as e:
            logger.debug("get_session error: %s", e)
            return None

    async def save_session(self, chat_id: int, session_id: str):
        try:
            await self._redis.set(f"{SESSION_KEY_PREFIX}{chat_id}", session_id)
        except Exception as e:
            logger.debug("save_session error: %s", e)

    async def push_reminders_dump(self):
        try:
            import json
            from reminders import get_db
            db = get_db()
            rows = db.conn.execute("SELECT * FROM reminders").fetchall()
            dump = [dict(r) for r in rows]
            await self._redis.set(REMINDERS_DUMP_KEY, json.dumps(dump))
            logger.info("Pushed reminders dump (%d items)", len(dump))
        except Exception as e:
            logger.debug("push_reminders_dump error: %s", e)

    async def pull_reminders_dump(self):
        try:
            import json
            raw = await self._redis.get(REMINDERS_DUMP_KEY)
            if not raw:
                return
            rows = json.loads(raw)
            if not rows:
                return
            from reminders import get_db
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
            logger.info("Pulled reminders dump (%d items)", len(rows))
        except Exception as e:
            logger.debug("pull_reminders_dump error: %s", e)

    async def send_heartbeat(self):
        try:
            await self._redis.set(HEARTBEAT_KEY, str(int(time.time())))
        except Exception as e:
            logger.debug("heartbeat error: %s", e)

    async def laptop_is_alive(self) -> bool:
        try:
            raw = await self._redis.get(HEARTBEAT_KEY)
            if not raw:
                return False
            ts = int(raw)
            return (time.time() - ts) < HEARTBEAT_TIMEOUT
        except Exception:
            return False

    async def run_laptop(self, start_bot_fn, stop_bot_fn):
        """Laptop main loop: heartbeat + sync from VPS + run bot."""
        await self.connect()
        await self.send_heartbeat()
        await self.pull_reminders_dump()

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._active = True

        try:
            await start_bot_fn()
        finally:
            self._active = False
            await self.push_reminders_dump()
            if self._heartbeat_task:
                self._heartbeat_task.cancel()

    async def run_vps(self, start_bot_fn, stop_bot_fn):
        """VPS main loop: watch heartbeat, activate when laptop dies."""
        await self.connect()

        while True:
            alive = await self.laptop_is_alive()
            if alive:
                if self._active:
                    logger.info("Laptop is back — deactivating VPS")
                    await self.push_reminders_dump()
                    self._active = False
                    await stop_bot_fn()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                continue

            if not self._active:
                logger.info("Laptop offline — activating VPS")
                await self.pull_reminders_dump()
                self._active = True
                try:
                    await start_bot_fn()
                except Exception as e:
                    logger.error("VPS start_bot failed: %s", e)
                    self._active = False
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _heartbeat_loop(self):
        try:
            while True:
                await self.send_heartbeat()
                await self.push_reminders_dump()
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            pass


class LeaseGateMiddleware:
    """Blocks ALL outgoing Telegram API calls when node is not active."""

    def __init__(self, node: FailoverNode):
        self._node = node

    async def __call__(self, make_request: Any, bot: Any, method) -> Any:
        if not self._node.is_active:
            logger.debug("Suppressed %s (not active)", type(method).__name__)
            return None
        return await make_request(bot, method)


async def _sync_repo(path: str, name: str) -> bool:
    cmds = [
        ["git", "-C", path, "fetch", "--quiet"],
        ["git", "-C", path, "reset", "--hard", "origin/main"],
        ["git", "-C", path, "clean", "-fd", "-e", ".mcp.json", "-e", ".env", "-e", "*.secret"],
    ]
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning("%s: sync failed at %s", name, cmd[2])
                return False
        except Exception as e:
            logger.warning("%s: sync error: %s", name, e)
            return False
    logger.info("%s: sync OK", name)
    return True
