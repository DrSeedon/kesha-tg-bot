"""One-off edit script for chat_state.py (V-44 steer). Kept as evidence of the change."""
p = 'chat_state.py'
s = open(p).read()


def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (s.count(a), a[:90])
    s = s.replace(a, b)


rep('''        self.compact_after_background: bool = False
''', '''        self.compact_after_background: bool = False
        # Messages that arrived mid-turn and will be steered into the CLI (V-44).
        self.steering: list[PendingEntry] = []
        self._steer_task: asyncio.Task | None = None
''')

# accept_entry + media_finished route through one helper
rep('''            if self.phase in (
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
''', '''            if self.phase in (
                ChatPhase.PROCESSING,
                ChatPhase.STOPPING,
                ChatPhase.COMPACTING,
            ):
                self._hold_during_turn(entry)
                return

            self.pending.append(entry)
            await self._arm_debounce()

    def _hold_during_turn(self, entry: PendingEntry) -> None:
        """An arrival while the chat is busy. MUST be called under lock.

        During a turn it is steered into the CLI, as Claude Code does with a
        message typed mid-turn; while stopping or compacting there is no turn
        to join, so it waits for the next batch.
        """
        if self.phase == ChatPhase.PROCESSING and callable(getattr(self.session, "steer", None)):
            self.steering.append(entry)
            if self._steer_task is None or self._steer_task.done():
                self._steer_task = asyncio.create_task(self._steer_after(self.debounce_sec))
            logger.info(f"Chat {self.chat_id}: steering 1 entry into the running turn")
            return
        self.deferred.append([entry])
        logger.info(f"Chat {self.chat_id}: deferred 1 entry during {self.phase}")

    async def _steer_after(self, delay: float) -> None:
        """Glue mid-turn arrivals like the debounce does, then hand them to the CLI."""
        await asyncio.sleep(delay)
        waited = 0.0
        while True:
            async with self._lock:
                # A voice note's text is the message: wait for it, like a batch does.
                if self.pending_transcriptions <= 0 or waited >= TRANSCRIPTION_WAIT_MAX:
                    batch, self.steering = self.steering, []
                    self._steer_task = None
                    phase = self.phase
                    break
            await asyncio.sleep(0.5)
            waited += 0.5
        if not batch:
            return
        if phase != ChatPhase.PROCESSING:
            await self._requeue(batch)
            return
        combined = self._compose(batch)
        # Same admission as a batch: a steer must not push the context past the
        # compact boundary. A refusal leaves it for the next batch, which runs
        # admission (and the compact) on its own.
        reserve = await self.session.check_context_reserve(combined)
        if reserve.get("should_compact") or (
            not reserve.get("ok")
            and reserve.get("reason") in ("reserve", "session_unavailable", "usage_limit")
        ):
            logger.info(
                f"Chat {self.chat_id}: steer refused by admission "
                f"({reserve.get('reason') or 'compact due'}), deferring"
            )
            await self._requeue(batch)
            return
        if not await self.session.steer(combined):
            await self._requeue(batch)
            return
        try:
            from message_log import get_db as _get_msg_db
            _get_msg_db().log_user(self.chat_id, combined, batch[-1].message_id)
        except Exception as e:
            logger.error(f"Chat {self.chat_id}: message_log user (steer) failed: {e}")
        try:
            from aiogram.enums import ChatAction
            await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
        except Exception as e:
            logger.debug(f"Chat {self.chat_id}: steer typing failed: {e}")

    async def _requeue(self, batch: list[PendingEntry]) -> None:
        """A steer that did not reach the CLI becomes an ordinary batch."""
        async with self._lock:
            if self._shutdown:
                return
            if self.phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING, ChatPhase.COMPACTING):
                self.deferred.append(batch)
                return
            self.pending.extend(batch)
            await self._arm_debounce()

    def _compose(self, batch: list[PendingEntry]) -> str:
        """The prompt text of a batch: KRSK time, msg ids, each message's body."""
        from datetime import datetime as _dt, timezone, timedelta

        krsk = timezone(timedelta(hours=7))
        # Use first entry's message for timestamp; reminder entries may have None message
        first_msg = next((e.message for e in batch if e.message is not None), None)
        if first_msg:
            batch_time = first_msg.date.astimezone(krsk).strftime("%Y-%m-%d %H:%M %z")
        else:
            batch_time = _dt.now(tz=krsk).strftime("%Y-%m-%d %H:%M %z")
        time_prefix = f"[{batch_time}] "
        if len(batch) == 1:
            return time_prefix + f"[msg_id={batch[0].message_id}] " + batch[0].prompt
        return time_prefix + "\\n\\n".join(
            f"--- message {i+1}/{len(batch)} [msg_id={e.message_id}] ---\\n{e.prompt}"
            for i, e in enumerate(batch)
        )
''')
rep('''                if self.phase in (
                    ChatPhase.PROCESSING,
                    ChatPhase.STOPPING,
                    ChatPhase.COMPACTING,
                ):
                    self.deferred.append([entry])
                    return
                self.pending.append(entry)''', '''                if self.phase in (
                    ChatPhase.PROCESSING,
                    ChatPhase.STOPPING,
                    ChatPhase.COMPACTING,
                ):
                    self._hold_during_turn(entry)
                    return
                self.pending.append(entry)''')
rep('''        try:
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
                combined = "\\n\\n".join(
                    f"--- message {i+1}/{len(batch)} [msg_id={e.message_id}] ---\\n{e.prompt}"
                    for i, e in enumerate(batch)
                )
                combined = time_prefix + combined
''', '''        try:
            combined = self._compose(batch)
''')
# drain: steering not yet handed over joins the deferred queue
rep('''        start_background = False
        async with self._lock:
            if self._shutdown:
                self.phase = ChatPhase.IDLE
                return''', '''        start_background = False
        async with self._lock:
            if self._shutdown:
                self.phase = ChatPhase.IDLE
                return
            if self.steering:
                # The turn ended before these were handed to the CLI.
                self.deferred.append(self.steering)
                self.steering = []''')
# /clear drops steering like deferred
rep('''            self.pending.clear()
            self.deferred.clear()
            self.cancel_requested = False''', '''            self.pending.clear()
            self.deferred.clear()
            self.steering.clear()
            self.cancel_requested = False''')
open(p, 'w').write(s)
print("ok")
