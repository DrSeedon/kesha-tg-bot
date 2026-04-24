"""ChatState — per-chat runtime state machine. Replaces all global dicts in bot.py."""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from aiogram import Bot, types
    from claude_session import ClaudeSession

logger = logging.getLogger("kesha")

TRANSCRIPTION_WAIT_MAX = 30  # seconds to wait for pending transcriptions before proceeding


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
    pending_model: "str | None"


class ChatState:
    def __init__(
        self,
        chat_id: int,
        session: "ClaudeSession",
        bot: "Bot",
        debounce_sec: int,
        auto_compact_pct: float,
        ask_fn,            # async ask_fn(message_or_none, prompt, chat_id)
        set_current_chat_fn,  # set_current_chat(chat_id)
        get_lazy_block_fn,    # get_lazy_block_for_prompt(chat_id) -> str
        compact_session_fn,   # async compact_session(session, notify) -> dict
        maybe_auto_compact_fn,  # async maybe_auto_compact(session, threshold, notify) -> dict|None
        work_dir: str,
    ):
        self.chat_id = chat_id
        self.session = session
        self.bot = bot
        self.debounce_sec = debounce_sec
        self.auto_compact_pct = auto_compact_pct
        self._ask_fn = ask_fn
        self._set_current_chat = set_current_chat_fn
        self._get_lazy_block = get_lazy_block_fn
        self._compact_session_fn = compact_session_fn
        self._maybe_auto_compact_fn = maybe_auto_compact_fn
        self._work_dir = work_dir

        self.phase: ChatPhase = ChatPhase.IDLE
        self.pending: list[PendingEntry] = []
        self.deferred: list[list[PendingEntry]] = []
        self.pending_transcriptions: int = 0
        self.cancel_requested: bool = False
        self.compact_requested: bool = False
        self.batch_message_ids: list[int] = []
        self.last_user_message_id: int | None = None
        self.generation: int = 0
        self.media_generation: int = 0
        self.pending_model: tuple[str, bool] | None = None

        self._debounce_task: asyncio.Task | None = None
        self._processing_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def is_busy(self) -> bool:
        """Sync, no lock — reads single field. True when Claude is active."""
        return self.phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING, ChatPhase.COMPACTING)

    # --- Public API ---

    async def accept_entry(self, entry: PendingEntry) -> None:
        """Enqueue a new user/reminder entry. Arms debounce or injects if already processing."""
        async with self._lock:
            if entry.source == "user" and entry.message_id:
                self.last_user_message_id = entry.message_id

            if self.phase == ChatPhase.STOPPING:
                self.deferred.append([entry])
                logger.info(f"Chat {self.chat_id}: queued 1 msg during STOPPING")
                return
            if self.phase == ChatPhase.PROCESSING:
                combined = entry.prompt
                logger.info(f"Chat {self.chat_id}: injecting 1 msg while processing ({len(combined)} chars)")
                inject_prompt = combined
                inject_id = entry.message_id
            elif self.phase == ChatPhase.COMPACTING:
                # Queue for after compaction
                self.deferred.append([entry])
                logger.info(f"Chat {self.chat_id}: queued 1 msg during compaction")
                return
            else:
                # IDLE, COLLECTING, WAITING_MEDIA — add to pending and (re)arm debounce
                self.pending.append(entry)
                await self._arm_debounce()
                return

        # Outside lock: do the inject I/O
        ok = await self.session.inject(inject_prompt)
        if not ok:
            logger.warning(f"Chat {self.chat_id}: inject failed, requeuing")
            async with self._lock:
                self.batch_message_ids.append(inject_id)
                self.deferred.append([entry])

    async def transcription_started(self) -> tuple[int, int]:
        """Call before starting an async transcription. Returns (generation, media_generation) snapshot."""
        async with self._lock:
            self.pending_transcriptions += 1
            return (self.generation, self.media_generation)

    async def transcription_finished(
        self,
        entry: PendingEntry | None,
        generation: int,
        media_generation: int,
    ) -> None:
        """Call after transcription completes. Discards stale results if /clear happened."""
        ready_batch: list[PendingEntry] | None = None
        async with self._lock:
            if generation != self.generation or media_generation != self.media_generation:
                logger.info(
                    f"Chat {self.chat_id}: stale transcription discarded "
                    f"(gen {generation}!={self.generation} or media_gen {media_generation}!={self.media_generation})"
                )
                self.pending_transcriptions = max(0, self.pending_transcriptions - 1)
                return
            self.pending_transcriptions = max(0, self.pending_transcriptions - 1)
            if entry is not None:
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

        if ready_batch:
            await self._start_processing(ready_batch)

    async def request_stop(self) -> bool:
        """Request stop of current processing. Returns True if there was something to stop."""
        async with self._lock:
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
        async with self._lock:
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
            self.pending_model = None
            self.batch_message_ids.clear()
            self.pending_transcriptions = 0
            self.generation += 1
            self.media_generation += 1
            prev = self.phase
            self.phase = ChatPhase.IDLE
            logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [request_clear gen={self.generation}]")

        # I/O outside lock
        await self.session.reset_async()
        return True

    async def request_compact(self) -> bool:
        """Request compaction. Deferred during PROCESSING/STOPPING, runs now from IDLE/COLLECTING/WAITING_MEDIA."""
        async with self._lock:
            if self.phase == ChatPhase.COMPACTING:
                return True
            if self.phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING):
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
                logger.info(f"Chat {self.chat_id}: compact cancelled {cancelled_count} pending transcriptions")
            prev = self.phase
            self.phase = ChatPhase.COMPACTING
            logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [request_compact]")
        await self._do_compact()
        return True

    async def run_urgent_prompt(self, prompt: str) -> None:
        """Run a reminder/urgent prompt. Injects if processing, starts new turn immediately if idle."""
        entry = PendingEntry(
            prompt=prompt,
            message_id=0,
            message=None,
            source="reminder",
            reply_target=self.chat_id,
        )
        start_now = False
        inject_prompt = entry.prompt
        async with self._lock:
            if self.phase == ChatPhase.PROCESSING:
                pass  # will inject below
            elif self.phase in (ChatPhase.STOPPING, ChatPhase.COMPACTING):
                self.deferred.append([entry])
                return
            elif self.phase == ChatPhase.IDLE:
                self.phase = ChatPhase.PROCESSING
                start_now = True
            else:
                self.pending.append(entry)
                return

        if start_now:
            await self._start_processing([entry])
            return
        ok = await self.session.inject(inject_prompt)
        if not ok:
            async with self._lock:
                self.deferred.append([entry])

    async def set_model(self, model_id: str, use_1m: bool) -> None:
        """Set model immediately or defer if processing."""
        async with self._lock:
            if self.phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING, ChatPhase.COMPACTING):
                self.pending_model = (model_id, use_1m)
                return
        self.session.model = model_id
        self.session.use_1m = use_1m
        await self.session.set_model_live(model_id)

    async def set_debounce(self, seconds: int) -> None:
        """Update debounce value. Already-armed timer runs with old value."""
        async with self._lock:
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
                pending_model=self.pending_model[0] if self.pending_model else None,
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

    async def _run_batch(self, batch: list[PendingEntry]) -> None:
        """Main processing loop. Runs OUTSIDE lock — lock acquired only for phase transitions."""
        import json as _json
        from datetime import timezone, timedelta

        self._set_current_chat(self.chat_id)
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

            try:
                lazy_block = self._get_lazy_block(self.chat_id)
                if lazy_block:
                    combined = lazy_block + combined
                    logger.info(f"Chat {self.chat_id}: injected lazy reminders block ({len(lazy_block)} chars)")
            except Exception as e:
                logger.error(f"Chat {self.chat_id}: lazy reminder injection failed: {e}")

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

            await self._ask_fn(reply_msg, combined, self.chat_id)

            # Auto-compact after response
            await self._maybe_auto_compact()

        except Exception as e:
            logger.error(f"Chat {self.chat_id} batch error: {e}", exc_info=True)
            try:
                await self.bot.send_message(self.chat_id, f"Bot error: {e}", parse_mode=None)
            except Exception:
                pass
        finally:
            await self._finish_processing()

    async def _finish_processing(self) -> None:
        """Finalize: apply pending_model, run deferred compact, transition to IDLE, drain deferred."""
        needs_compact = False
        model_id = None
        use_1m = False
        async with self._lock:
            if self.pending_model is not None:
                model_id, use_1m = self.pending_model
                self.pending_model = None
            if self.compact_requested:
                needs_compact = True
                self.compact_requested = False

            self.cancel_requested = False
            self.batch_message_ids.clear()

        if model_id is not None:
            try:
                self.session.model = model_id
                self.session.use_1m = use_1m
                await self.session.set_model_live(model_id)
                logger.info(f"Chat {self.chat_id}: deferred model change applied: {model_id}")
            except Exception as e:
                logger.error(f"Chat {self.chat_id}: deferred model change failed: {e}")

        if needs_compact:
            async with self._lock:
                self.phase = ChatPhase.COMPACTING
            await self._do_compact()
        else:
            await self._drain_or_idle()

    async def _maybe_auto_compact(self) -> None:
        """Run auto-compact if threshold exceeded. Sets COMPACTING phase during compact."""
        if self.auto_compact_pct <= 0:
            return
        async with self._lock:
            self.phase = ChatPhase.COMPACTING
        try:
            async def _notify(text):
                await self.bot.send_message(self.chat_id, text)
            result = await self._maybe_auto_compact_fn(self.session, self.auto_compact_pct, notify=_notify)
            if result and result.get("ok"):
                logger.info(
                    f"Chat {self.chat_id}: auto-compact ok, "
                    f"{result.get('before_pct', 0):.1f}% → {result.get('after_pct', 0):.1f}%"
                )
        except Exception as e:
            logger.error(f"Chat {self.chat_id}: auto-compact failed: {e}", exc_info=True)
        finally:
            async with self._lock:
                if self.phase == ChatPhase.COMPACTING:
                    self.phase = ChatPhase.PROCESSING  # restore — _finish_processing sets IDLE

    async def _do_compact(self) -> None:
        """Execute manual compaction (from request_compact). Called outside lock."""
        try:
            async def _notify(text):
                await self.bot.send_message(self.chat_id, text)
            result = await self._compact_session_fn(self.session, notify=_notify)
            if result and result.get("ok"):
                logger.info(
                    f"Chat {self.chat_id}: compact ok, "
                    f"{result.get('before_pct', 0):.1f}% → {result.get('after_pct', 0):.1f}%"
                )
        except Exception as e:
            logger.error(f"Chat {self.chat_id}: compact failed: {e}", exc_info=True)
        finally:
            async with self._lock:
                self.compact_requested = False
            await self._drain_or_idle()

    async def _drain_or_idle(self) -> None:
        """Atomically: if deferred exists → PROCESSING + drain, else → IDLE. No IDLE window."""
        async with self._lock:
            if self.deferred:
                merged: list[PendingEntry] = []
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
                    return
            elif self.pending:
                self.phase = ChatPhase.IDLE
                await self._arm_debounce()
                return
            else:
                prev = self.phase
                self.phase = ChatPhase.IDLE
                logger.info(f"Chat {self.chat_id}: phase {prev} → {self.phase} [idle]")
                return
        await self._start_processing(merged)

    async def _drain_deferred(self) -> None:
        """Process queued deferred batches after PROCESSING/COMPACTING ends."""
        async with self._lock:
            if not self.deferred:
                return
            # Merge all deferred batches
            merged: list[PendingEntry] = []
            for b in self.deferred:
                merged.extend(b)
            self.deferred.clear()
            if not merged:
                return
            self.phase = ChatPhase.PROCESSING

        await self._start_processing(merged)

    async def _try_inject(self, batch: list[PendingEntry]) -> bool:
        """Try to inject batch into running Claude turn. Returns True if successful."""
        combined = "\n\n".join(e.prompt for e in batch)
        ok = await self.session.inject(combined)
        if ok:
            async with self._lock:
                self.batch_message_ids.extend(e.message_id for e in batch if e.message_id)
        return ok


class ChatRegistry:
    """Creates and caches ChatState per chat_id. Single source of truth for all chats."""

    def __init__(
        self,
        bot,
        mcp_config: dict,
        system_prompt: str,
        model: str,
        debounce_sec: int,
        auto_compact_pct: float,
        ask_fn,
        set_current_chat_fn,
        get_lazy_block_fn,
        compact_session_fn,
        maybe_auto_compact_fn,
        work_dir: str,
    ):
        self._chats: dict[int, ChatState] = {}
        self._bot = bot
        self._mcp_config = mcp_config
        self._system_prompt = system_prompt
        self._model = model
        self._debounce_sec = debounce_sec
        self._auto_compact_pct = auto_compact_pct
        self._ask_fn = ask_fn
        self._set_current_chat = set_current_chat_fn
        self._get_lazy_block = get_lazy_block_fn
        self._compact_session_fn = compact_session_fn
        self._maybe_auto_compact_fn = maybe_auto_compact_fn
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
            )
            self._chats[chat_id] = ChatState(
                chat_id=chat_id,
                session=session,
                bot=self._bot,
                debounce_sec=self._debounce_sec,
                auto_compact_pct=self._auto_compact_pct,
                ask_fn=self._ask_fn,
                set_current_chat_fn=self._set_current_chat,
                get_lazy_block_fn=self._get_lazy_block,
                compact_session_fn=self._compact_session_fn,
                maybe_auto_compact_fn=self._maybe_auto_compact_fn,
                work_dir=self._work_dir,
            )
            logger.info(f"ChatRegistry: created ChatState for chat {chat_id}")
        return self._chats[chat_id]

    async def shutdown(self) -> None:
        for chat in self._chats.values():
            if chat._debounce_task and not chat._debounce_task.done():
                chat._debounce_task.cancel()
            if chat._processing_task and not chat._processing_task.done():
                chat._processing_task.cancel()
        self._chats.clear()
