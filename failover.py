"""Failover module — distributed lease management for Kesha bot."""

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
import redis.asyncio as aioredis
from aiogram.methods.base import TelegramMethod

from config import logger

# ─── Constants ───────────────────────────────────────────────────────────────

RENEW_INTERVAL = 30        # seconds between renewals
LEASE_TTL = 120            # Redis key TTL
LOCAL_TTL = 45             # local deadline after last successful renew
DRAIN_DEADLINE = 60        # max seconds for graceful drain
HEALTH_RELEASE_STRIKES = 6 # consecutive unhealthy ticks before voluntary release

LEASE_KEY = "kesha:lease"
PRIORITY_KEY = "kesha:preferred_node"

# ─── Lua Scripts ─────────────────────────────────────────────────────────────

_LUA_ACQUIRE = """
local raw = redis.call("GET", KEYS[1])
if raw then
    local d = cjson.decode(raw)
    if d.owner ~= cjson.null then return 0 end
end
local epoch = 1
local merged = cjson.decode(ARGV[3])
if raw then
    local d = cjson.decode(raw)
    epoch = (d.epoch or 0) + 1
    if d.sessions then
        for k, v in pairs(d.sessions) do
            if type(v) == "table" and v.ts then
                if not merged[k] or not merged[k].ts or v.ts > merged[k].ts then
                    merged[k] = v
                end
            end
        end
    end
end
local lease = cjson.encode({
    owner = ARGV[1], epoch = epoch, healthy = true,
    draining = false, handback_to = cjson.null,
    sessions = merged, updated_at = tonumber(ARGV[4])
})
redis.call("SET", KEYS[1], lease, "EX", tonumber(ARGV[2]))
return epoch
"""

_LUA_RENEW = """
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner ~= ARGV[1] or d.epoch ~= tonumber(ARGV[2]) then return -1 end
d.healthy = ARGV[3] == "true"
d.updated_at = tonumber(ARGV[5])
local incoming = cjson.decode(ARGV[4])
for k, v in pairs(incoming) do
    if type(v) == "table" and v.ts then
        if not d.sessions[k] or not d.sessions[k].ts or v.ts >= d.sessions[k].ts then
            d.sessions[k] = v
        end
    end
end
redis.call("SET", KEYS[1], cjson.encode(d), "EX", tonumber(ARGV[6]))
if d.handback_to ~= cjson.null then return 2 end
return 1
"""

_LUA_RELEASE = """
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner ~= ARGV[1] or d.epoch ~= tonumber(ARGV[2]) then return 0 end
local incoming = cjson.decode(ARGV[3])
for k, v in pairs(incoming) do
    if type(v) == "table" and v.ts then
        if not d.sessions[k] or not d.sessions[k].ts or v.ts >= d.sessions[k].ts then
            d.sessions[k] = v
        end
    end
end
d.owner = cjson.null; d.draining = false; d.handback_to = cjson.null
d.updated_at = tonumber(ARGV[4])
redis.call("SET", KEYS[1], cjson.encode(d), "EX", tonumber(ARGV[5]))
return 1
"""

_LUA_REQUEST_HANDBACK = """
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner == ARGV[1] then return 0 end
if d.owner == cjson.null then return -1 end
d.handback_to = ARGV[1]
redis.call("SET", KEYS[1], cjson.encode(d), "KEEPTTL")
return d.epoch
"""

_LUA_START_DRAIN = """
local raw = redis.call("GET", KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.owner ~= ARGV[1] or d.epoch ~= tonumber(ARGV[2]) then return 0 end
d.draining = true
redis.call("SET", KEYS[1], cjson.encode(d), "KEEPTTL")
return 1
"""

# ─── LeaseGateMiddleware ──────────────────────────────────────────────────────

class LeaseGateMiddleware:
    """Blocks outgoing Telegram API calls when not lease owner (except GetMe)."""

    _PASSTHROUGH = frozenset({"GetMe"})

    def __init__(self, lease_manager: "LeaseManager") -> None:
        self._lm = lease_manager

    async def __call__(self, make_request: Any, bot: Any, method: TelegramMethod) -> Any:
        method_name = type(method).__name__
        if self._lm and not self._lm.is_owner_now and method_name not in self._PASSTHROUGH:
            logger.debug("Suppressed %s (not lease owner)", method_name)
            return None
        return await make_request(bot, method)


# ─── LeaseManager ────────────────────────────────────────────────────────────

class LeaseManager:
    """Distributed lease owner with Redis coordination and local deadline."""

    RENEW_INTERVAL = RENEW_INTERVAL
    LEASE_TTL = LEASE_TTL
    LOCAL_TTL = LOCAL_TTL
    DRAIN_DEADLINE = DRAIN_DEADLINE
    HEALTH_RELEASE_STRIKES = HEALTH_RELEASE_STRIKES

    def __init__(self, node_id: str, redis_url: str, priority: str = "primary") -> None:
        self.node_id = node_id
        self.priority = priority
        self._redis_url = redis_url
        self._redis: aioredis.Redis | None = None
        self.epoch: int = 0
        self._is_owner: bool = False
        self._lease_valid_until: float = 0.0
        self._unhealthy_streak: int = 0
        self._polling_task: asyncio.Task | None = None
        self._reminder_task: asyncio.Task | None = None
        self._reminder_stop: asyncio.Event = asyncio.Event()
        self._registry: Any = None  # ChatRegistry, set externally

    @property
    def is_owner_now(self) -> bool:
        return self._is_owner and time.time() < self._lease_valid_until

    async def run(
        self,
        on_acquire: Callable[[int, dict], Awaitable[None]],
        on_release: Callable[[], Awaitable[None]],
        on_lost: Callable[[], Awaitable[None]],
    ) -> None:
        """Main loop. Never exits unless cancelled."""
        await self._connect_redis()

        while True:
            try:
                if self._is_owner:
                    await self._owner_tick(on_release, on_lost)
                else:
                    await self._standby_tick(on_acquire)
            except Exception as e:
                logger.error("LeaseManager tick error: %s", e, exc_info=True)
                if self._is_owner:
                    await self._fail_closed(on_lost)
            await asyncio.sleep(self.RENEW_INTERVAL)

    # ─── Owner tick ──────────────────────────────────────────────────────────

    async def _owner_tick(
        self,
        on_release: Callable[[], Awaitable[None]],
        on_lost: Callable[[], Awaitable[None]],
    ) -> None:
        if self._polling_task and self._polling_task.done():
            exc = self._polling_task.exception() if not self._polling_task.cancelled() else None
            logger.error("Polling task died: %s", exc)
            await self._fail_closed(on_lost)
            return

        health_ok = await self._check_health()
        if health_ok:
            self._unhealthy_streak = 0
        else:
            self._unhealthy_streak += 1

        sessions = self._build_sessions()
        healthy = self._unhealthy_streak < 3

        try:
            result = await self._lua_renew(healthy, sessions)
        except Exception:
            await self._fail_closed(on_lost)
            return

        if result in (-1, 0):
            await self._fail_closed(on_lost)
        elif result == 2:
            await self._graceful_release(on_release, on_lost)
        else:
            self._lease_valid_until = time.time() + self.LOCAL_TTL

        if self._unhealthy_streak >= self.HEALTH_RELEASE_STRIKES:
            await self._graceful_release(on_release, on_lost)

    # ─── Priority helpers ──────────────────────────────────────────────────

    async def _is_preferred(self) -> bool:
        try:
            preferred = await self._redis.get(PRIORITY_KEY)
            if preferred:
                return preferred == self.node_id
        except Exception:
            pass
        return self.priority == "primary"

    async def set_preferred_node(self, node_id: str) -> None:
        await self._redis.set(PRIORITY_KEY, node_id)
        logger.info("Preferred node set to: %s", node_id)

    # ─── Standby tick ────────────────────────────────────────────────────────

    async def _standby_tick(self, on_acquire: Callable[[int, dict], Awaitable[None]]) -> None:
        preferred = await self._is_preferred()

        if not preferred:
            try:
                import json
                raw = await self._redis.get(LEASE_KEY)
                if raw:
                    data = json.loads(raw)
                    if data.get("owner") not in (None, "null"):
                        return
            except Exception:
                pass

        if preferred and not self._is_owner:
            try:
                result = await self._lua_request_handback()
                if result > 0:
                    logger.info("Requested handback (preferred node)")
            except Exception:
                pass

        try:
            epoch = await self._lua_acquire(self._build_sessions())
        except Exception:
            return

        if epoch <= 0:
            return

        self.epoch = epoch
        self._is_owner = True
        self._lease_valid_until = time.time() + self.LOCAL_TTL
        self._unhealthy_streak = 0

        sessions = await self._get_sessions_from_lease()
        try:
            await on_acquire(epoch, sessions)
            # Renew immediately after acquire — don't wait 30s for next tick
            try:
                await self._lua_renew(healthy=True, sessions=self._build_sessions())
                self._lease_valid_until = time.time() + self.LOCAL_TTL
            except Exception:
                pass
        except Exception as e:
            logger.error("on_acquire failed: %s", e, exc_info=True)
            await self._emergency_stop()
            self._is_owner = False
            self._lease_valid_until = 0.0
            try:
                await self._lua_release(self._build_sessions())
            except Exception:
                pass

    # ─── Fail closed ─────────────────────────────────────────────────────────

    async def _fail_closed(self, on_lost: Callable[[], Awaitable[None]]) -> None:
        """Any error while owning → full stop. Does NOT release lease."""
        self._is_owner = False
        self._lease_valid_until = 0.0
        await self._emergency_stop()
        try:
            await on_lost()
        except Exception:
            pass

    async def _emergency_stop(self) -> None:
        """Stop polling + reminders immediately."""
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._polling_task), timeout=5)
            except BaseException:
                pass
        self._reminder_stop.set()
        if self._reminder_task and not self._reminder_task.done():
            self._reminder_task.cancel()
            try:
                await asyncio.wait_for(self._reminder_task, timeout=5)
            except BaseException:
                pass

    # ─── Graceful release ────────────────────────────────────────────────────

    async def _graceful_release(
        self,
        on_release: Callable[[], Awaitable[None]],
        on_lost: Callable[[], Awaitable[None]],
    ) -> None:
        """Drain with TTL renewal, then release. On failure → fail_closed."""
        drain_renew = asyncio.create_task(self._drain_renew_loop())
        try:
            await self._lua_start_drain()
            await asyncio.wait_for(on_release(), timeout=self.DRAIN_DEADLINE)
            await self._lua_release(self._build_sessions())
            self._is_owner = False
            self._lease_valid_until = 0.0
        except BaseException as e:
            logger.error("Graceful release failed: %s", e)
            await self._fail_closed(on_lost)
            raise
        finally:
            drain_renew.cancel()
            try:
                await drain_renew
            except asyncio.CancelledError:
                pass

    async def _drain_renew_loop(self) -> None:
        """Renew TTL every 15s while draining."""
        try:
            while True:
                await asyncio.sleep(15)
                try:
                    await self._lua_renew(healthy=True, sessions=self._build_sessions())
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    # ─── Session helpers ─────────────────────────────────────────────────────

    def _build_sessions(self) -> dict:
        """Collect current session_id + ts from all chats via registry."""
        if self._registry is None:
            return {}
        result: dict = {}
        try:
            for chat_id, cs in self._registry._chats.items():
                sid = cs.session.session_id if cs.session else None
                ts = getattr(cs.session, "session_id_changed_at", 0) if cs.session else 0
                if sid:
                    result[str(chat_id)] = {"id": sid, "ts": ts}
        except Exception as e:
            logger.debug("_build_sessions error: %s", e)
        return result

    async def _get_sessions_from_lease(self) -> dict:
        """Read sessions payload from current lease key."""
        try:
            import json
            raw = await self._redis.get(LEASE_KEY)
            if raw:
                data = json.loads(raw)
                return data.get("sessions", {})
        except Exception as e:
            logger.debug("_get_sessions_from_lease error: %s", e)
        return {}

    # ─── Lua callers ─────────────────────────────────────────────────────────

    async def _lua_acquire(self, sessions: dict) -> int:
        import json
        return await self._redis.eval(
            _LUA_ACQUIRE, 1, LEASE_KEY,
            self.node_id,
            str(self.LEASE_TTL),
            json.dumps(sessions),
            str(int(time.time())),
        )

    async def _lua_renew(self, healthy: bool, sessions: dict) -> int:
        import json
        return await self._redis.eval(
            _LUA_RENEW, 1, LEASE_KEY,
            self.node_id,
            str(self.epoch),
            "true" if healthy else "false",
            json.dumps(sessions),
            str(int(time.time())),
            str(self.LEASE_TTL),
        )

    async def _lua_release(self, sessions: dict) -> int:
        import json
        return await self._redis.eval(
            _LUA_RELEASE, 1, LEASE_KEY,
            self.node_id,
            str(self.epoch),
            json.dumps(sessions),
            str(int(time.time())),
            str(self.LEASE_TTL),
        )

    async def _lua_request_handback(self) -> int:
        return await self._redis.eval(
            _LUA_REQUEST_HANDBACK, 1, LEASE_KEY,
            self.node_id,
        )

    async def _lua_start_drain(self) -> int:
        return await self._redis.eval(
            _LUA_START_DRAIN, 1, LEASE_KEY,
            self.node_id,
            str(self.epoch),
        )

    # ─── Redis connection ─────────────────────────────────────────────────────

    async def _connect_redis(self) -> None:
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        logger.info("LeaseManager: Redis connected (%s)", self._redis_url)

    # ─── Health check ─────────────────────────────────────────────────────────

    async def _check_health(self) -> bool:
        try:
            proxy = os.environ.get("HTTPS_PROXY")
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession() as s:
                kw: dict[str, Any] = {"timeout": timeout}
                if proxy:
                    kw["proxy"] = proxy
                async with s.get("https://api.telegram.org/", **kw) as r:
                    return r.status < 500
        except Exception:
            return False


# ─── Git helpers ──────────────────────────────────────────────────────────────

async def _sync_repo(path: str, name: str) -> bool:
    """Force-sync repo to remote: fetch + reset --hard. Local config files excluded."""
    cmds = [
        ["git", "-C", path, "fetch", "--quiet"],
        ["git", "-C", path, "reset", "--hard", "origin/main"],
        ["git", "-C", path, "clean", "-fd", "-e", ".mcp.json", "-e", ".env", "-e", "*.secret"],
    ]
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                logger.warning("%s: sync failed at %s", name, cmd[2])
                return False
        except Exception as e:
            logger.warning("%s: sync error at %s: %s", name, cmd[2], e)
            return False
    logger.info("%s: sync OK", name)
    return True


async def _push_repo(path: str, name: str) -> None:
    """Best-effort push. Failures logged but don't block drain."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "push", "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            logger.info("%s: pushed OK", name)
        else:
            logger.warning("%s: push returned non-zero (non-critical)", name)
    except Exception as e:
        logger.warning("%s: push failed (non-critical): %s", name, e)


# ─── Drain orchestrator ───────────────────────────────────────────────────────

async def drain_for_handback(registry: Any, lease_manager: LeaseManager) -> None:
    """on_release callback. Stops polling, shuts down all chats, stops reminders, pushes repos."""
    from aiogram import Dispatcher

    dp: Dispatcher | None = getattr(registry, "_dp", None)
    if dp is not None:
        await dp.stop_polling()

    tasks = [
        asyncio.create_task(cs.enter_shutdown())
        for cs in list(registry._chats.values())
    ]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=55)
        for t in pending:
            t.cancel()

    lease_manager._reminder_stop.set()
    if lease_manager._reminder_task and not lease_manager._reminder_task.done():
        try:
            await asyncio.wait_for(lease_manager._reminder_task, timeout=5)
        except asyncio.TimeoutError:
            lease_manager._reminder_task.cancel()
        except Exception:
            pass

    bot_repo = str(Path(__file__).parent.resolve())
    await _push_repo(bot_repo, "kesha-bot")
