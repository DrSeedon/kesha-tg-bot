"""ChatState — per-chat runtime state machine. Replaces all global dicts in bot.py."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from config import (
    AUTO_COMPACT_IDLE,
    AUTO_COMPACT_MIN_CONTEXT_PCT,
    AUTO_COMPACT_TZ,
    AUTO_COMPACT_WINDOW_END,
    AUTO_COMPACT_WINDOW_START,
    STRINGS,
    t as _t_cfg,
)
from message_log import ActivityPersistenceError

if TYPE_CHECKING:
    from aiogram import Bot, types
    from claude_session import ClaudeSession
    from message_log import MessageLog

logger = logging.getLogger("kesha")

TRANSCRIPTION_WAIT_MAX = 30  # seconds to wait for pending transcriptions before proceeding


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_auto_compact_night(now_utc: datetime) -> bool:
    local_time = now_utc.astimezone(AUTO_COMPACT_TZ).time().replace(tzinfo=None)
    return (
        local_time >= AUTO_COMPACT_WINDOW_START
        or local_time < AUTO_COMPACT_WINDOW_END
    )


def _seconds_until_night(now_utc: datetime) -> float:
    local = now_utc.astimezone(AUTO_COMPACT_TZ)
    if _is_auto_compact_night(now_utc):
        return 0.0
    target = datetime.combine(
        local.date(),
        AUTO_COMPACT_WINDOW_START,
        tzinfo=AUTO_COMPACT_TZ,
    )
    return max(0.0, (target.astimezone(timezone.utc) - now_utc).total_seconds())


class ChatPhase(StrEnum):
    IDLE = "idle"
    COLLECTING = "collecting"        # debounce timer running
    WAITING_MEDIA = "waiting_media"  # debounce elapsed, transcriptions pending
    PROCESSING = "processing"        # Claude turn active
    STOPPING = "stopping"            # interrupt requested, waiting for stream end
    COMPACTING = "compacting"        # context compaction in progress


@dataclass(slots=True)
class PendingEntry:
    prompt: str
    message_id: int
    message: "types.Message | None" = None  # None for reminder/system entries
    source: Literal["user", "reminder"] = "user"
    reply_target: "int | None" = None  # chat_id to send response to


@dataclass(slots=True)
class ChatSnapshot:
    chat_id: int
    phase: ChatPhase
    pending_count: int
    deferred_batches: int
    pending_transcriptions: int
    cancel_requested: bool
    compact_requested: bool
    generation: int
    session_id: "str | None"
    model: str


class ChatState:
    def __init__(
        self,
        chat_id: int,
        session: "ClaudeSession",
        bot: "Bot",
        debounce_sec: int,
        ask_fn,            # async ask_fn(message_or_none, prompt, chat_id)
        set_current_chat_fn,  # set_current_chat(chat_id)
        get_lazy_block_fn,    # get_lazy_block_for_prompt(chat_id) -> str
        compact_session_fn,   # async compact_session(session, notify) -> dict
        activity_store: "MessageLog",
        work_dir: str,
    ):
        self.chat_id = chat_id
        self.session = session
        self.bot = bot
        self.debounce_sec = debounce_sec
        self._ask_fn = ask_fn
        self._set_current_chat = set_current_chat_fn
        self._get_lazy_block = get_lazy_block_fn
        self._compact_session_fn = compact_session_fn
        self._activity_store = activity_store
        self._work_dir = work_dir

        self.phase: ChatPhase = ChatPhase.IDLE
        self.pending: list[PendingEntry] = []
        self.deferred: list[list[PendingEntry]] = []
        self.pending_transcriptions: int = 0
        self.cancel_requested: bool = False
        self.compact_requested: bool = False
        self.compact_requested_automatic: bool = False
        self.batch_message_ids: list[int] = []
        self.last_user_message_id: int | None = None
        self.generation: int = 0
        self.media_generation: int = 0
        self._debounce_task: asyncio.Task | None = None
        self._processing_task: asyncio.Task | None = None
        self._auto_compact_task: asyncio.Task | None = None
        self._compact_started: bool = False
        self._context_reserve_blocked: bool = False
        self._lock = asyncio.Lock()
        self._shutdown: bool = False

    @property
    def is_busy(self) -> bool:
        """Sync, no lock — reads single field. True when Claude is active."""
        return self.phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING, ChatPhase.COMPACTING)

    # --- Public API ---

    async def accept_entry(self, entry: PendingEntry) -> None:
        """Durably admit an entry, deferring every arrival during an active turn."""
        self._cancel_auto_compact()
        self._activity_store.begin_activity(self.chat_id)
        async with self._lock:
            if self._shutdown:
                raise RuntimeError("chat is shutting down")
            if entry.source == "user" and entry.message_id:
                self.last_user_message_id = entry.message_id

            if self.phase in (
                ChatPhase.PROCESSING,
                ChatPhase.STOPPING,
                ChatPhase.COMPACTING,
            ):
                self.deferred.append([entry])
                logger.info(
                    "Chat %s: deferred 1 entry during %s",
                    self.chat_id,
                    self.phase,
                )
                return

            self.pending.append(entry)
            await self._arm_debounce()

    async def _send_batch_terminal(
        self,
        batch: list[PendingEntry],
        key: str,
    ) -> None:
        entry = batch[-1]
        if entry.message is not None:
            await entry.message.answer(_t_cfg(entry.message, key), parse_mode=None)
            return
        await self.bot.send_message(
            entry.reply_target or self.chat_id,
            STRINGS["ru"][key],
            parse_mode=None,
        )

    async def mark_context_reserve_blocked(self) -> None:
        async with self._lock:
            self._context_reserve_blocked = True

    async def media_started(self) -> tuple[int, int]:
        """Persist activity before starting asynchronous media work."""
        self._cancel_auto_compact()
        self._activity_store.begin_activity(self.chat_id)
        async with self._lock:
            if self._shutdown:
                raise RuntimeError("chat is shutting down")
            self.pending_transcriptions += 1
            return (self.generation, self.media_generation)

    async def media_finished(
        self,
        entry: PendingEntry | None,
        generation: int,
        media_generation: int,
    ) -> None:
        """Complete media work and defer it when another Claude turn is active."""
        ready_batch: list[PendingEntry] | None = None
        finish_idle = False
        async with self._lock:
            if self._shutdown:
                self.pending_transcriptions = max(0, self.pending_transcriptions - 1)
                return
            if generation != self.generation or media_generation != self.media_generation:
                logger.info(
                    f"Chat {self.chat_id}: stale transcription discarded "
                    f"(gen {generation}!={self.generation} or media_gen {media_generation}!={self.media_generation})"
                )
                self.pending_transcriptions = max(0, self.pending_transcriptions - 1)
                return
            self.pending_transcriptions = max(0, self.pending_transcriptions - 1)
            if entry is not None:
                if self.phase in (
                    ChatPhase.PROCESSING,
                    ChatPhase.STOPPING,
                    ChatPhase.COMPACTING,
                ):
                    self.deferred.append([entry])
                    return
                self.pending.append(entry)
            if self.pending_transcriptions == 0:
                if self.phase == ChatPhase.WAITING_MEDIA:
                    ready_batch = list(self.pending)
                    self.pending.clear()
                    if self._debounce_task and not self._debounce_task.done():
                        self._debounce_task.cancel()
                        self._debounce_task = None
                    self.phase = ChatPhase.PROCESSING
                elif self.phase == ChatPhase.IDLE and self.pending:
                    await self._arm_debounce()
                    return
                elif self.phase == ChatPhase.IDLE:
                    finish_idle = True

        if ready_batch:
            await self._start_processing(ready_batch)
        elif finish_idle:
            await self._drain_or_idle()

    async def request_stop(self) -> bool:
        """Request stop of current processing. Returns True if there was something to stop."""
        async with self._lock:
            if self._shutdown: return False
            if self.phase not in (ChatPhase.PROCESSING, ChatPhase.STOPPING):
                return False
            prev = self.phase
            self.cancel_requested = True
            self.phase = ChatPhase.STOPPING
            logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [request_stop]")
        await self.session.interrupt()
        return True

    async def request_clear(self) -> bool:
        """Clear session. Rejected during PROCESSING/COMPACTING/STOPPING. Returns True if cleared."""
        self._cancel_auto_compact()
        self._activity_store.begin_activity(self.chat_id)
        async with self._lock:
            if self._shutdown: return False
            if self.phase in (ChatPhase.PROCESSING, ChatPhase.COMPACTING, ChatPhase.STOPPING):
                return False
            # Cancel debounce timer
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
                self._debounce_task = None
            self.pending.clear()
            self.deferred.clear()
            self.cancel_requested = False
            self.compact_requested = False
            self.compact_requested_automatic = False
            self.batch_message_ids.clear()
            self.pending_transcriptions = 0
            self.generation += 1
            self.media_generation += 1
            prev = self.phase
            self.phase = ChatPhase.IDLE
            logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [request_clear gen={self.generation}]")

        # I/O outside lock
        await self.session.reset_async()
        self._context_reserve_blocked = False
        try:
            self._activity_store.finish_activity(self.chat_id)
        except ActivityPersistenceError as exc:
            logger.error(f"Chat {self.chat_id}: clear completion persistence failed: {exc}")
        return True

    async def request_compact(self, automatic: bool = False) -> bool:
        """Request compaction; deferred requests preserve manual-over-automatic provenance."""
        if not automatic:
            self._cancel_auto_compact()
            self._activity_store.begin_activity(self.chat_id)
        async with self._lock:
            if self._shutdown:
                return False
            if automatic and getattr(self.session, "usage_limit_active", False):
                logger.info(f"Chat {self.chat_id}: automatic compact skipped (usage limit)")
                return True
            if self.phase == ChatPhase.COMPACTING:
                if not automatic and self.compact_requested_automatic:
                    self.compact_requested = True
                    self.compact_requested_automatic = False
                return True
            if self.phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING):
                if not self.compact_requested:
                    self.compact_requested_automatic = automatic
                else:
                    self.compact_requested_automatic = (
                        self.compact_requested_automatic and automatic
                    )
                self.compact_requested = True
                return True
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
                self._debounce_task = None
            if self.pending:
                self.deferred.append(list(self.pending))
                self.pending.clear()
            if self.pending_transcriptions > 0:
                cancelled_count = self.pending_transcriptions
                self.media_generation += 1
                self.pending_transcriptions = 0
                logger.info(
                    f"Chat {self.chat_id}: compact cancelled "
                    f"{cancelled_count} pending transcriptions"
                )
            prev = self.phase
            self.phase = ChatPhase.COMPACTING
            logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [request_compact]")
        await self._do_compact(automatic=automatic)
        return True

    async def run_urgent_prompt(self, prompt: str) -> None:
        """Run an urgent reminder now when idle, otherwise defer it safely."""
        self._cancel_auto_compact()
        self._activity_store.begin_activity(self.chat_id)
        entry = PendingEntry(
            prompt=prompt,
            message_id=0,
            message=None,
            source="reminder",
            reply_target=self.chat_id,
        )
        start_now = False
        async with self._lock:
            if self._shutdown:
                raise RuntimeError("chat is shutting down")
            if self.phase in (
                ChatPhase.PROCESSING,
                ChatPhase.STOPPING,
                ChatPhase.COMPACTING,
            ):
                self.deferred.append([entry])
                return
            if self.phase == ChatPhase.IDLE:
                self.phase = ChatPhase.PROCESSING
                start_now = True
            else:
                self.pending.append(entry)

        if start_now:
            await self._start_processing([entry])

    async def set_debounce(self, seconds: int) -> None:
        """Update debounce value. Already-armed timer runs with old value."""
        async with self._lock:
            if self._shutdown: return
            self.debounce_sec = seconds

    def should_stop(self) -> bool:
        return self.cancel_requested

    async def get_snapshot(self) -> ChatSnapshot:
        async with self._lock:
            return ChatSnapshot(
                chat_id=self.chat_id,
                phase=self.phase,
                pending_count=len(self.pending),
                deferred_batches=len(self.deferred),
                pending_transcriptions=self.pending_transcriptions,
                cancel_requested=self.cancel_requested,
                compact_requested=self.compact_requested,
                generation=self.generation,
                session_id=self.session.session_id,
                model=self.session.model,
            )

    # --- Internal state machine ---

    

    async def _arm_debounce(self) -> None:
        """(Re)arm debounce timer. MUST be called under lock."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        prev = self.phase
        self.phase = ChatPhase.COLLECTING
        logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [arm_debounce]")
        self._debounce_task = asyncio.create_task(self._on_debounce_elapsed())

    async def _on_debounce_elapsed(self) -> None:
        """Debounce timer coroutine. Runs outside lock."""
        try:
            await asyncio.sleep(self.debounce_sec)
        except asyncio.CancelledError:
            return

        # Wait for pending transcriptions
        waited = 0.0
        while True:
            async with self._lock:
                if self.pending_transcriptions <= 0:
                    break
                if waited >= TRANSCRIPTION_WAIT_MAX:
                    logger.warning(
                        f"Chat {self.chat_id}: transcription timeout after {waited:.1f}s, "
                        f"{self.pending_transcriptions} still pending — proceeding anyway"
                    )
                    self.pending_transcriptions = 0
                    self.media_generation += 1
                    break
                self.phase = ChatPhase.WAITING_MEDIA
            await asyncio.sleep(0.5)
            waited += 0.5

        async with self._lock:
            self._debounce_task = None
            if self.phase not in (ChatPhase.COLLECTING, ChatPhase.WAITING_MEDIA):
                return
            batch = list(self.pending)
            self.pending.clear()
            if not batch:
                prev = self.phase
                self.phase = ChatPhase.IDLE
                logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [debounce_empty]")
                return
            prev = self.phase
            self.phase = ChatPhase.PROCESSING
            logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [debounce_fire batch={len(batch)}]")

        await self._start_processing(batch)

    async def _start_processing(self, batch: list[PendingEntry]) -> None:
        """Kick off _run_batch as a tracked task."""
        self._processing_task = asyncio.create_task(self._run_batch(batch))

    def _cancel_auto_compact(self) -> None:
        if self.phase == ChatPhase.COMPACTING and self._compact_started:
            return
        task = self._auto_compact_task
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._auto_compact_task = None

    def _arm_auto_compact(self) -> None:
        self._cancel_auto_compact()
        if self._shutdown or not self.session.session_id:
            return
        try:
            row = self._activity_store.get_activity(self.chat_id)
        except Exception as exc:
            logger.error(f"Chat {self.chat_id}: activity read failed: {exc}")
            return
        if (
            not row
            or not row["quiescent"]
            or row["auto_attempted_for_utc"] == row["last_activity_utc"]
        ):
            return
        self._auto_compact_task = asyncio.create_task(
            self._run_auto_compact_scheduler()
        )

    async def _run_auto_compact_scheduler(self) -> None:
        try:
            while not self._shutdown:
                row = self._activity_store.get_activity(self.chat_id)
                if (
                    not row
                    or not row["quiescent"]
                    or row["auto_attempted_for_utc"] == row["last_activity_utc"]
                ):
                    return
                last_activity = datetime.fromisoformat(row["last_activity_utc"])
                now = _utc_now()
                idle_delay = (
                    last_activity + AUTO_COMPACT_IDLE - now
                ).total_seconds()
                delay = max(0.0, idle_delay)
                if delay == 0 and not _is_auto_compact_night(now):
                    delay = _seconds_until_night(now)
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                await self._reserve_automatic_probe(row["last_activity_utc"])
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                f"Chat {self.chat_id}: auto-compact scheduler failed: {exc}",
                exc_info=True,
            )
        finally:
            if self._auto_compact_task is asyncio.current_task():
                self._auto_compact_task = None

    async def _reserve_automatic_probe(self, last_activity_utc: str) -> None:
        async with self._lock:
            row = self._activity_store.get_activity(self.chat_id)
            eligible = (
                row
                and row["last_activity_utc"] == last_activity_utc
                and row["quiescent"]
                and row["auto_attempted_for_utc"] is None
                and self.phase == ChatPhase.IDLE
                and not self.pending
                and not self.deferred
                and self.pending_transcriptions == 0
                and not self.compact_requested
                and _is_auto_compact_night(_utc_now())
                and not getattr(self.session, "usage_limit_active", False)
            )
            if not eligible:
                return
            if not self._activity_store.claim_auto_attempt(
                self.chat_id, last_activity_utc
            ):
                return
            self.compact_requested = True
            self.compact_requested_automatic = True
            self._compact_started = False
            self.phase = ChatPhase.COMPACTING

        try:
            usage = await self.session.get_context_usage(
                refresh=True,
                preserve_session=True,
            )
        except asyncio.CancelledError:
            async with self._lock:
                if self.compact_requested_automatic:
                    self.compact_requested = False
                    self.compact_requested_automatic = False
            await asyncio.shield(self._drain_or_idle(record_activity=False))
            raise
        except Exception as exc:
            logger.error(f"Chat {self.chat_id}: context probe failed: {exc}")
            usage = None
        pct = usage.get("percentage") if usage else None

        start_compact = False
        automatic = True
        async with self._lock:
            row = self._activity_store.get_activity(self.chat_id)
            manual = self.compact_requested and not self.compact_requested_automatic
            runtime_work = bool(
                self.pending
                or self.deferred
                or self.pending_transcriptions
            )
            activity_changed = bool(
                not row
                or not row["quiescent"]
                or row["last_activity_utc"] != last_activity_utc
            )
            automatic_eligible = (
                not runtime_work
                and not activity_changed
                and _is_auto_compact_night(_utc_now())
                and not getattr(self.session, "usage_limit_active", False)
                and pct is not None
                and pct >= AUTO_COMPACT_MIN_CONTEXT_PCT
            )
            if manual and not runtime_work:
                start_compact = True
                automatic = False
            elif automatic_eligible:
                start_compact = True
            elif not manual:
                self.compact_requested = False
                self.compact_requested_automatic = False

        if start_compact:
            self._compact_started = True
            await self._do_compact(automatic=automatic)
        else:
            await self._drain_or_idle(record_activity=False)

    async def _run_batch(self, batch: list[PendingEntry]) -> None:
        """Main processing loop. Runs OUTSIDE lock — lock acquired only for phase transitions."""
        from datetime import timezone, timedelta

        async with self._lock:
            self.batch_message_ids = [
                e.message_id for e in batch if e.message_id
            ]

        try:
            krsk = timezone(timedelta(hours=7))
            # Use first entry's message for timestamp; reminder entries may have None message
            first_msg = next((e.message for e in batch if e.message is not None), None)
            if first_msg:
                batch_time = first_msg.date.astimezone(krsk).strftime("%Y-%m-%d %H:%M %z")
                time_prefix = f"[{batch_time}] "
            else:
                from datetime import datetime as _dt
                time_prefix = f"[{_dt.now(tz=krsk).strftime('%Y-%m-%d %H:%M %z')}] "

            if len(batch) == 1:
                combined = time_prefix + f"[msg_id={batch[0].message_id}] " + batch[0].prompt
            else:
                combined = "\n\n".join(
                    f"--- message {i+1}/{len(batch)} [msg_id={e.message_id}] ---\n{e.prompt}"
                    for i, e in enumerate(batch)
                )
                combined = time_prefix + combined

            lazy_ids = []
            lazy_reschedule = []
            try:
                lazy_block, lazy_ids, lazy_reschedule = self._get_lazy_block(self.chat_id)
                if lazy_block:
                    combined = lazy_block + combined
                    logger.info(f"Chat {self.chat_id}: injected lazy reminders block ({len(lazy_block)} chars)")
            except Exception as e:
                logger.error(f"Chat {self.chat_id}: lazy reminder injection failed: {e}")

            if self._context_reserve_blocked:
                reserve = {"ok": False, "reason": "reserve"}
            else:
                reserve = await self.session.check_context_reserve(combined)
            if not reserve.get("ok"):
                reason = reserve.get("reason")
                if reason == "reserve":
                    await self.mark_context_reserve_blocked()
                    key = "context_reserve"
                elif reason == "session_unavailable":
                    key = "session_unavailable"
                else:
                    key = "context_unknown"
                logger.warning(
                    "Chat %s: batch rejected before query (%s)",
                    self.chat_id,
                    reason,
                )
                await self._send_batch_terminal(batch, key)
                return

            previews = []
            for e in batch:
                p = e.prompt
                if "[photo:" in p:
                    previews.append("photo")
                elif "[voice:" in p:
                    previews.append("voice")
                elif "[video_note:" in p:
                    previews.append("videonote")
                elif "[video:" in p:
                    previews.append("video")
                elif "[document:" in p:
                    previews.append("doc")
                elif "[audio:" in p:
                    previews.append("audio")
                elif "[sticker:" in p:
                    previews.append("sticker")
                else:
                    txt = p.split("]: ", 1)[-1][:40].replace("\n", " ")
                    previews.append(f'"{txt}"')
            logger.info(
                f"Chat {self.chat_id}: sending {len(batch)} msgs [{', '.join(previews)}] ({len(combined)} chars)"
            )

            # Determine reply target — last user message (or bot.send_message for reminders)
            last_entry = batch[-1]
            reply_msg = last_entry.message  # may be None for reminders

            try:
                from message_log import get_db as _get_msg_db
                _get_msg_db().log_user(self.chat_id, combined, batch[-1].message_id if batch else 0)
            except Exception as e:
                logger.error(f"Chat {self.chat_id}: message_log user failed: {e}")

            await self._ask_fn(reply_msg, combined, self.chat_id)

            if lazy_ids:
                try:
                    from reminders import mark_lazy_delivered
                    mark_lazy_delivered(lazy_ids, lazy_reschedule)
                except Exception as e:
                    logger.error(f"Chat {self.chat_id}: mark_lazy_delivered failed: {e}")

        except Exception as e:
            logger.error(f"Chat {self.chat_id} batch error: {e}", exc_info=True)
            try:
                await self.bot.send_message(self.chat_id, f"Bot error: {e}", parse_mode=None)
            except Exception:
                pass
        finally:
            await self._finish_processing()

    async def _finish_processing(self) -> None:
        """Finalize: run deferred compact, transition to IDLE, drain deferred."""
        logger.info(f"Chat {self.chat_id}: _finish_processing start")
        needs_compact = False
        automatic = False
        async with self._lock:
            if self.compact_requested:
                needs_compact = True
                automatic = self.compact_requested_automatic
                self.compact_requested = False
                self.compact_requested_automatic = False

            self.cancel_requested = False
            self.batch_message_ids.clear()

        if needs_compact:
            async with self._lock:
                self.phase = ChatPhase.COMPACTING
            await self._do_compact(automatic=automatic)
        else:
            await self._drain_or_idle()

    def _make_compact_notifier(self):
        progress_message_id = None

        async def notify(text: str, *, replace: bool = False):
            nonlocal progress_message_id
            if not replace:
                return await self.bot.send_message(self.chat_id, text)

            if progress_message_id is not None:
                try:
                    await self.bot.edit_message_text(
                        text,
                        chat_id=self.chat_id,
                        message_id=progress_message_id,
                    )
                    return None
                except Exception as exc:
                    if "message is not modified" in str(exc).lower():
                        return None
                    logger.warning(
                        f"Chat {self.chat_id}: compact progress edit failed: {exc}"
                    )
                    try:
                        await self.bot.delete_message(self.chat_id, progress_message_id)
                    except Exception:
                        pass

            progress = await self.bot.send_message(self.chat_id, text)
            progress_message_id = progress.message_id if progress else None
            return progress

        return notify

    async def _do_compact(self, automatic: bool = False) -> None:
        """Execute one custom compact request outside the state lock."""
        result = None
        try:
            if automatic and getattr(self.session, "usage_limit_active", False):
                logger.info(
                    f"Chat {self.chat_id}: deferred automatic compact skipped (usage limit)"
                )
                return
            if not automatic:
                reserve = await self.session.check_context_reserve(manual=True)
                if not reserve.get("ok"):
                    reason = reserve.get("reason")
                    key = (
                        "compact_floor"
                        if reason == "reserve"
                        else "session_unavailable"
                        if reason == "session_unavailable"
                        else "context_unknown"
                    )
                    logger.warning(
                        "Chat %s: manual compact rejected before query (%s)",
                        self.chat_id,
                        reason,
                    )
                    await self.bot.send_message(
                        self.chat_id,
                        STRINGS["ru"][key],
                        parse_mode=None,
                    )
                    return
            result = await self._compact_session_fn(
                self.session,
                notify=self._make_compact_notifier(),
            )
            if result and result.get("ok"):
                async with self._lock:
                    self._context_reserve_blocked = False
                logger.info(
                    f"Chat {self.chat_id}: compact ok, "
                    f"{result.get('before_pct', 0):.1f}% → "
                    f"{result.get('after_pct', 0):.1f}%"
                )
        except Exception as e:
            logger.error(f"Chat {self.chat_id}: compact failed: {e}", exc_info=True)
        finally:
            async with self._lock:
                self.compact_requested = False
                self.compact_requested_automatic = False
                self._compact_started = False
            await self._drain_or_idle(record_activity=not automatic)

    async def _drain_or_idle(self, *, record_activity: bool = True) -> None:
        """Atomically: if deferred exists → PROCESSING + drain, else → IDLE. No IDLE window."""
        merged: list[PendingEntry] | None = None
        arm_auto = False
        async with self._lock:
            if self._shutdown:
                self.phase = ChatPhase.IDLE
                return
            if self.deferred:
                merged = []
                for b in self.deferred:
                    merged.extend(b)
                self.deferred.clear()
                if merged:
                    prev = self.phase
                    self.phase = ChatPhase.PROCESSING
                    logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [drain_deferred n={len(merged)}]")
                else:
                    prev = self.phase
                    self.phase = ChatPhase.IDLE
                    logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [drain_empty]")
                    merged = None
            elif self.pending:
                self.phase = ChatPhase.IDLE
                await self._arm_debounce()
                return
            else:
                if record_activity:
                    try:
                        self._activity_store.finish_activity(self.chat_id)
                    except ActivityPersistenceError as exc:
                        logger.error(
                            f"Chat {self.chat_id}: completion persistence failed: {exc}"
                        )
                        self.phase = ChatPhase.IDLE
                        return
                prev = self.phase
                self.phase = ChatPhase.IDLE
                logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [idle]")
                arm_auto = record_activity
        if merged:
            await self._start_processing(merged)
        elif arm_auto:
            self._arm_auto_compact()



class ChatRegistry:
    """Creates and caches ChatState per chat_id. Single source of truth for all chats."""

    def __init__(
        self,
        bot,
        mcp_config: dict,
        system_prompt: str,
        model: str,
        debounce_sec: int,
        ask_fn,
        set_current_chat_fn,
        get_lazy_block_fn,
        compact_session_fn,
        activity_store: "MessageLog",
        work_dir: str,
    ):
        self._chats: dict[int, ChatState] = {}
        self._bot = bot
        self._mcp_config = mcp_config
        self._system_prompt = system_prompt
        self._model = model
        self._debounce_sec = debounce_sec
        self._ask_fn = ask_fn
        self._set_current_chat = set_current_chat_fn
        self._get_lazy_block = get_lazy_block_fn
        self._compact_session_fn = compact_session_fn
        self._activity_store = activity_store
        self._work_dir = work_dir

    def get(self, chat_id: int) -> ChatState:
        if chat_id not in self._chats:
            from claude_session import ClaudeSession
            session_file = Path(__file__).parent / "storage" / "sessions" / str(chat_id)
            session = ClaudeSession(
                cwd=self._work_dir,
                model=self._model,
                system_prompt=self._system_prompt,
                mcp_servers=self._mcp_config,
                session_file=session_file,
                on_connecting=lambda: self._set_current_chat(chat_id),
            )
            self._chats[chat_id] = ChatState(
                chat_id=chat_id,
                session=session,
                bot=self._bot,
                debounce_sec=self._debounce_sec,
                ask_fn=self._ask_fn,
                set_current_chat_fn=self._set_current_chat,
                get_lazy_block_fn=self._get_lazy_block,
                compact_session_fn=self._compact_session_fn,
                activity_store=self._activity_store,
                work_dir=self._work_dir,
            )
            logger.info(f"ChatRegistry: created ChatState for chat {chat_id}")
        return self._chats[chat_id]

    async def start_auto_compact(self) -> None:
        """Restore one scheduler per durable quiescent chat after a process restart."""
        for chat_id in self._activity_store.list_quiescent_chat_ids():
            session_file = Path(__file__).parent / "storage" / "sessions" / str(chat_id)
            if not session_file.exists() or not session_file.read_text().strip():
                continue
            self.get(chat_id)._arm_auto_compact()

    async def shutdown(self) -> None:
        tasks = []
        for chat in self._chats.values():
            chat._shutdown = True
            if chat._debounce_task and not chat._debounce_task.done():
                chat._debounce_task.cancel()
                tasks.append(chat._debounce_task)
            if chat._processing_task and not chat._processing_task.done():
                chat._processing_task.cancel()
                tasks.append(chat._processing_task)
            if chat._auto_compact_task and not chat._auto_compact_task.done():
                chat._auto_compact_task.cancel()
                tasks.append(chat._auto_compact_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for chat in self._chats.values():
            await chat.session._safe_disconnect()
        self._chats.clear()
