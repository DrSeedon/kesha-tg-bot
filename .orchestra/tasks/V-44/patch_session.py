"""One-off edit script for claude_session.py (V-44 steer). Kept as evidence of the change."""
p = 'claude_session.py'
s = open(p).read()


def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (s.count(a), a[:90])
    s = s.replace(a, b)


rep('''        self.agents_open: set[str] = set()
        self._consumers = 0''', '''        self.agents_open: set[str] = set()
        # A reader that will see a steered prompt's turn through to its Result.
        self.reader_open = False
        self._consumers = 0''')
rep('''    def has_unread_turn(self) -> bool:
        return self.turn_open or any(self.opens_turn(msg) for msg in self.frames)
''', '''    def has_unread_turn(self) -> bool:
        return self.turn_open or any(self.opens_turn(msg) for msg in self.frames)

    def close_reader(self) -> None:
        """No reader is left to answer prompts: forget the ones still owed.

        Their turns, if the CLI still runs them, arrive as CLI turns and are
        delivered by the background reader.
        """
        self.reader_open = False
        self.pending.clear()

    def nudge(self) -> None:
        """Make a waiting reader re-check `pending` (a steer was withdrawn)."""
        self.frames.append(_NUDGE)
        self._wake.set()
''')
rep('''_STREAM_END = object()
''', '''_STREAM_END = object()
_NUDGE = object()
''')
rep('''        stream = self._stream
        stream.attach()
        try:
            async for chunk in self._turn_chunks(stream, None):
                yield chunk
        except Exception as exc:
            logger.error("Background turn stream failed: %s", exc)
            if self._client is stream.client:
                self._connected = False
        finally:
            stream.detach()''', '''        stream = self._stream
        stream.attach()
        stream.reader_open = True
        try:
            async for chunk in self._turn_chunks(stream, None):
                yield chunk
        except Exception as exc:
            stream.close_reader()
            logger.error("Background turn stream failed: %s", exc)
            if self._client is stream.client:
                self._connected = False
        finally:
            stream.close_reader()
            stream.detach()

    async def steer(self, text: str) -> bool:
        """Hand a user message to the CLI while a turn is being read.

        The CLI merges it into the running turn at the next tool boundary or
        runs it as the next turn (V-43 research §2, prompts B and E) — what
        Claude Code does with a message typed mid-turn. The attached reader
        waits for its echo and Result, so the answer reaches the same reply.
        False means nothing was sent: queue it instead.
        """
        stream = self._stream
        if not (self._pump_alive() and stream.reader_open):
            return False
        prompt_uuid = str(uuid.uuid4())
        stream.pending[prompt_uuid] = False  # before any await: the reader must wait for it
        try:
            async with self._query_lock:
                if self._client is not stream.client or not stream.reader_open:
                    raise RuntimeError("reader closed before the steer was written")
                await self._client.query(self._user_frames(text, prompt_uuid))
        except Exception as exc:
            stream.pending.pop(prompt_uuid, None)
            stream.nudge()
            logger.warning(f"Steer not delivered, queueing: {exc}")
            return False
        logger.info(f"Steered into the running turn: {text[:80]}...")
        return True''')
rep('''            stream = self._attach_stream()
            stream.attach()
            prompt_uuid = str(uuid.uuid4())''', '''            stream = self._attach_stream()
            stream.attach()
            stream.reader_open = True
            prompt_uuid = str(uuid.uuid4())''')
rep('''        except Exception as e:
            err = str(e)
            if isinstance(e, ResultError) and e.subtype == "error_max_turns":''', '''        except Exception as e:
            if stream is not None:
                stream.close_reader()
            err = str(e)
            if isinstance(e, ResultError) and e.subtype == "error_max_turns":''')
rep('''        finally:
            if stream is not None:
                stream.detach()
            if self._is_processing:''', '''        finally:
            if stream is not None:
                stream.close_reader()
                stream.detach()
            if self._is_processing:''')
rep('''        while True:
            awaiting_pickup = (
                prompt_uuid is not None
                and stream.pending.get(prompt_uuid) is False
                and not stream.turn_open
            )
            if prompt_uuid is None and not stream.frames and not stream.turn_open:
                return
''', '''        while True:
            # Prompts owed an answer: ours plus any steered in meanwhile. The
            # reader ends only once all of them were taken and their turns closed.
            awaiting_pickup = not stream.turn_open and not all(stream.pending.values())
            if not stream.turn_open and not stream.pending and (
                prompt_uuid is not None or not stream.frames
            ):
                return
''')
rep('''            except (asyncio.TimeoutError, TimeoutError):
                # The CLI is between turns and still has not taken the prompt:
                # it never will (an echo contract change, a lost write). Waiting
                # on would hold the chat in PROCESSING forever.
                stream.pending.pop(prompt_uuid, None)
                logger.error(
                    "CLI stayed idle %.0fs without taking prompt %s",
                    PROMPT_PICKUP_TIMEOUT_S, prompt_uuid[:8],
                )''', '''            except (asyncio.TimeoutError, TimeoutError):
                # The CLI is between turns and still has not taken the prompt:
                # it never will (an echo contract change, a lost write). Waiting
                # on would hold the chat in PROCESSING forever.
                lost = [uid for uid, taken in stream.pending.items() if not taken]
                if not lost:
                    continue
                for uid in lost:
                    stream.pending.pop(uid, None)
                logger.error(
                    "CLI stayed idle %.0fs without taking prompt(s) %s",
                    PROMPT_PICKUP_TIMEOUT_S, [uid[:8] for uid in lost],
                )''')
rep('''
            if msg is _STREAM_END:
                if stream.error is not None:''', '''
            if msg is _NUDGE:
                continue
            if msg is _STREAM_END:
                if stream.error is not None:''')
rep('''                msg = await stream.next()
                if msg is _STREAM_END:''', '''                msg = await stream.next()
                if msg is _NUDGE:
                    continue
                if msg is _STREAM_END:''')
rep('''                ours = prompt_uuid is not None and prompt_uuid in stream.turn_uids''', '''                # Ours: the turn took a prompt we sent (the question or a steer).
                ours = bool(stream.turn_uids)''')
rep('''                if ours:
                    return
                yield {"type": "turn_done", "ours": False}''', '''                if prompt_uuid is not None and not stream.pending:
                    return
                yield {"type": "turn_done", "ours": ours}''')
open(p, 'w').write(s)
print("ok")
