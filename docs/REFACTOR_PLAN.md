# Refactoring Plan: kesha-tg-bot (merged)

> Combined plan from Claude Opus + Codex GPT-5.4 dual review.
> Claude provided precise line references and migration map.
> Codex provided deeper state machine design, test strategy, and safety mechanisms.

## 1. Target Architecture

```
kesha-tg-bot/
  bot.py                # ~100 lines: bootstrap, dp/bot creation, main(), entrypoint
  config.py             # ~80 lines: env, constants, STRINGS, t(), logging setup
  chat_state.py         # ~250 lines: ChatPhase, PendingEntry, ChatState, ChatRegistry
  response_stream.py    # ~250 lines: _ask() — streaming, drafts, ToolStatusTracker, retries
  pipeline.py           # ~150 lines: enqueue(), _debounce_fire(), _process_batch()
  media.py              # ~180 lines: download, transcribe, cache, cleanup
  handlers.py           # ~300 lines: all @dp.message handlers + set_commands()
  telegram_io.py        # ~80 lines: _send_safe(), split_msg(), typing_loop(), user_prefix(), etc.
  claude_session.py     # unchanged
  tool_status.py        # unchanged
  compact.py            # unchanged
  kesha_tools.py        # minor: use ChatRegistry instead of _bot_ref globals
  reminders.py          # minor: call ChatRegistry for urgent_llm instead of probing _is_processing
```

**Core rule:** only `ChatState` mutates per-chat runtime. Handlers, reminders, MCP tools call ChatState methods — never touch globals.

## 2. ChatState Design

### Types

```python
from enum import StrEnum
from dataclasses import dataclass
from typing import Literal, Optional
import asyncio

class ChatPhase(StrEnum):
    IDLE = "idle"
    COLLECTING = "collecting"       # debounce timer running
    WAITING_MEDIA = "waiting_media" # debounce elapsed, transcriptions pending
    PROCESSING = "processing"      # Claude turn active
    STOPPING = "stopping"          # interrupt requested, waiting for stream end
    COMPACTING = "compacting"      # context compaction in progress

@dataclass(slots=True)
class PendingEntry:
    prompt: str
    message_id: int
    message: "types.Message | None" = None  # None for reminder/system entries
    source: Literal["user", "reminder"] = "user"
    reply_target: int | None = None  # chat_id to send response to (always set)
```

### ChatState class

```python
class ChatState:
    def __init__(self, chat_id: int, session: ClaudeSession, bot: Bot,
                 debounce_sec: int, auto_compact_pct: float):
        self.chat_id = chat_id
        self.session = session
        self.bot = bot
        self.debounce_sec = debounce_sec
        self.auto_compact_pct = auto_compact_pct

        self.phase: ChatPhase = ChatPhase.IDLE
        self.pending: list[PendingEntry] = []
        self.deferred: list[list[PendingEntry]] = []
        self.pending_transcriptions: int = 0
        self.cancel_requested: bool = False
        self.compact_requested: bool = False
        self.batch_message_ids: list[int] = []
        self.last_user_message_id: int | None = None
        self.generation: int = 0  # incremented on /clear — stale callbacks check this
        self.media_generation: int = 0  # incremented on /clear AND WAITING_MEDIA timeout — stale transcription callbacks check this
        self.pending_model: tuple[str, bool] | None = None  # (model_id, use_1m) deferred during PROCESSING

        self._debounce_task: asyncio.Task | None = None
        self._processing_task: asyncio.Task | None = None  # owned task for shutdown cleanup
        self._lock = asyncio.Lock()

    # --- Public API (called by handlers, reminders, tools) ---
    # All public methods acquire _lock internally for state transitions.
    # NEVER hold _lock across _run_batch/_ask or long I/O.
    async def accept_entry(self, entry: PendingEntry) -> None: ...
    async def transcription_started(self) -> None: ...
    async def transcription_finished(self, entry: PendingEntry | None, generation: int) -> None: ...
    async def request_stop(self) -> bool: ...
    async def request_clear(self) -> bool: ...
    async def request_compact(self) -> bool: ...
    async def run_urgent_prompt(self, prompt: str) -> None: ...
    async def set_model(self, model: str, use_1m: bool) -> None: ...
    async def set_debounce(self, seconds: int) -> None: ...
    async def get_snapshot(self) -> "ChatSnapshot": ...  # acquires _lock briefly

    # --- Internal (state machine) ---
    # Lock protocol: acquire lock → read/mutate phase + fields → release lock → do I/O.
    # Pattern: snapshot state under lock, release, do work, reacquire to finalize.
    async def _arm_debounce(self) -> None: ...
    async def _on_debounce_elapsed(self) -> None: ...
    async def _start_processing(self, batch: list[PendingEntry]) -> None: ...
    async def _run_batch(self, batch: list[PendingEntry]) -> None:
        # Runs OUTSIDE lock. Lock acquired only for phase transitions at start/end.
        ...
    async def _finish_processing(self) -> None:
        # Under lock: apply pending_model if set, transition to IDLE, drain deferred.
        ...
    async def _maybe_auto_compact(self) -> None: ...
    async def _drain_deferred(self) -> None: ...
    async def _try_inject(self, batch: list[PendingEntry]) -> bool: ...
```

### State transitions

```
IDLE ──(entry)──> COLLECTING ──(timer + no transcriptions)──> PROCESSING ──(done)──> IDLE
                      │                                            │
                      │ (timer + transcriptions pending)           ├──(auto-compact)──> COMPACTING ──> IDLE
                      v                                            │
                 WAITING_MEDIA ──(all transcriptions done)────>    │
                                                                   │
                                          /stop ──> STOPPING ─────┘

During PROCESSING: inject or queue to deferred
During COMPACTING: always queue to deferred
/clear during PROCESSING/COMPACTING: rejected
After PROCESSING/COMPACTING ends: drain deferred → new PROCESSING or IDLE
```

### ChatRegistry

```python
class ChatRegistry:
    def __init__(self, bot, mcp_config, system_prompt, model, debounce, auto_compact):
        self._chats: dict[int, ChatState] = {}
        self._bot = bot
        # ... shared config

    def get(self, chat_id: int) -> ChatState:
        if chat_id not in self._chats:
            session = ClaudeSession(cwd=..., model=..., ...)
            self._chats[chat_id] = ChatState(chat_id, session, self._bot, ...)
        return self._chats[chat_id]
```

### ChatSnapshot (for /status and diagnostics)

```python
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
    session_id: str | None
    model: str
    pending_model: str | None
```

### Why asyncio.Lock

Single-threaded asyncio means most operations don't race. But `await` points inside state transitions (e.g. `session.inject()`, `session.interrupt()`) create yield points where another coroutine could mutate state. The lock serializes transitions — not for thread safety, but for coroutine safety at yield points. Lightweight, zero overhead when uncontested.

## 3. File Breakdown — What Goes Where

Source: current bot.py line numbers (v1.7.2)

| New file | From bot.py lines | What |
|----------|-------------------|------|
| config.py | 1-44, 46-79, 83-198, 200-209, 1137-1143 | Env, logging, STRINGS, t(), ALLOWED_MODELS |
| telegram_io.py | 335-412, 415-420, 539-552, 734-759, 762-775 | user_prefix, forward_meta, reply_meta, extract_*_urls, typing_loop, split_msg, _send_safe, draft helpers |
| media.py | 214-260, 231-272, 247-256, 424-503, 534-537 | download_file, transcribe (now aiohttp), caches, cleanup, media_count, log_size |
| chat_state.py | New file (replaces lines 557-564, 698) | ChatPhase, PendingEntry, ChatState, ChatRegistry |
| pipeline.py | 567-568, 574-605, 608-696, 701-731 | enqueue, _debounce_fire, _process_batch — adapted to ChatState |
| response_stream.py | 779-968 | _ask(chat_id, prompt, reply_message: Message \| None) — streaming, drafts, tool status, retries. When reply_message=None (reminders), uses bot.send_message(chat_id, ...) instead of message.answer() |
| handlers.py | 971-1387 | All @dp.message handlers, command lists, set_commands() |
| bot.py | 276-296, 298-314, 1389-1509 | Bootstrap: bot/dp creation, _load_global_mcp, main(), singleton lock |

## 4. Migration Strategy

### Phase 0: Stabilization (30 min, independent deploy)

Before moving code:
1. Guard `/ping` with `allowed()` check (currently unguarded)
2. Remove `_resolve_chat()` fallback-to-first-allowed — tools must have explicit chat context or fail
3. Add `python -c "import bot"` as pre-restart smoke test habit

### Phase 1: Extract ChatState — replace globals (1 hour)

Highest value. All race conditions die here. No files move.

1. Create `chat_state.py` with ChatPhase, PendingEntry, ChatState, ChatRegistry
2. In `bot.py`: `from chat_state import ChatRegistry`; create registry in `main()`
3. Delete 8 global dicts/sets
4. Replace ~50 access points using **ChatState public API only** (no direct field mutation from outside):
   - `_pending[cid].append(entry); _debounce_fire(cid)` → `await registry.get(cid).accept_entry(entry)`
   - `cid in _processing` → `registry.get(cid).get_snapshot().phase in (ChatPhase.PROCESSING, ChatPhase.STOPPING)` or add `@property is_busy -> bool` on ChatState
   - `_cancel.add(cid); session.interrupt()` → `await registry.get(cid).request_stop()`
   - `_compacting.add(cid)` → `await registry.get(cid).request_compact()`
   - `/clear` handler → `await registry.get(cid).request_clear()`
   - `set_model(...)` → `await registry.get(cid).set_model(model, use_1m)`
   - `DEBOUNCE_SEC = val` → `await registry.get(cid).set_debounce(val)`
   - Status display → `registry.get(cid).get_snapshot()`
   - No code outside `ChatState` touches `.phase`, `.pending`, `.deferred`, `.cancel_requested` directly.
5. Update kesha_tools.py: use `registry.get(chat_id)` API methods, not field access
6. Update reminders.py: `await registry.get(chat_id).run_urgent_prompt(payload)` — no `_is_processing` probing

**Verification:**
- `python -c "import bot"` passes
- `grep -rE '_processing|_compacting|_pending\b|_queue\b|_cancel\b|current_batch_message_ids' *.py --exclude=chat_state.py` = 0 hits (repo-wide, not just bot.py)
- Manual test: text, voice, photo, album, /clear, /stop, /compact, /status, tool calls

### Phase 2: Extract utility modules (1.5 hours)

Mechanical extraction. Each sub-step independently deployable.

1. **config.py** — constants, env, logging, STRINGS, t()
2. **telegram_io.py** — pure message utilities, _send_safe, draft helpers
3. **media.py** — download, transcribe, caches, cleanup

Bot object: use `set_bot()` pattern (already proven in kesha_tools.py).

**Circular import prevention:** bot is created in config.py (or bot.py exports it). media.py and telegram_io.py receive it via `set_bot()` called in `main()`.

### Phase 3: Extract pipeline + handlers (1 hour)

1. **response_stream.py** — _ask() moves as-is (closures over local state move with it)
2. **pipeline.py** — enqueue, debounce, process_batch (now thin wrappers over ChatState)
3. **handlers.py** — all handlers + `register(dp)` function

**Critical:** preserve handler registration order (media_group before photo, text before fallback).

4. **Slim bot.py** — ~100 lines: bootstrap, main(), singleton lock

### Phase 4: Hardening (optional, 30 min)

1. Add structured transition logs: `chat_id, from_phase, event, to_phase`
2. `/status` uses `ChatState` snapshot directly
3. `ClaudeSession._is_processing` becomes read-only property (no external mutation)

## 5. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Missing state access (~50 points) | Medium | Bot crash | grep for old globals = 0 hits; Python NameError catches immediately |
| Queue drain behavior change | Medium | Message loss | Line-by-line comparison of drain logic |
| Circular imports (Phase 2) | High | Import crash | set_bot() late binding; smoke test before restart |
| Handler registration order | Low | Wrong handler fires | Copy exact order, test all media types |
| Hot deploy crash | Medium | Bot downtime | `python -c "import bot"` before every restart; `git stash` rollback |
| Lock contention | Very Low | Slow response | asyncio Lock = zero overhead when uncontested (99.9% of the time) |

### Testing strategy

Manual tests (minimum per phase):
- [ ] Text message → response
- [ ] Voice message → transcription → response
- [ ] Photo with caption → response
- [ ] Album (multi-photo) → response
- [ ] Message during processing → injection
- [ ] /stop during processing → interrupt
- [ ] /clear → session reset
- [ ] /compact → compaction
- [ ] /status → shows correct info
- [ ] Tool call visible in status bubble
- [ ] Reminder fires correctly

Future (optional): pytest + fake ClaudeSession for CI, but not blocking the refactor.

## 6. What NOT to Change

- **claude_session.py** — clean, single-responsibility SDK wrapper
- **tool_status.py** — self-contained tracker, well-tested after v1.7.0 fixes
- **compact.py** — pure functions, clean interface
- **reminders.py** — ReminderDB + scheduler (only rewire the _urgent_llm path)
- **Streaming/draft approach** — complex but correct after v1.7.2 fixes
- **Debounce+inject architecture** — the design is sound, bugs were in state tracking
- **system_prompt.txt** — content
- No Redis, Celery, actor frameworks, or DB-backed queues. This is a 2-user bot.

## 7. Effort Summary

| Phase | What | Time | Risk | Deploys |
|-------|------|------|------|---------|
| 0 | Security fixes, habits | 30min | None | Yes |
| 1 | **ChatState** (kills all races) | 1h | Medium | Yes |
| 2 | config.py, telegram_io.py, media.py | 1.5h | Low | Yes (each) |
| 3 | response_stream.py, pipeline.py, handlers.py | 1h | Low | Yes |
| 4 | Structured logs, hardening | 30min | None | Yes |
| **Total** | | **4.5h** | | |

**Phase 1 alone delivers 80% of the value** (all race conditions fixed). Everything after is readability.

## Design Decisions Log

| Decision | Chosen | Alternative | Why |
|----------|--------|-------------|-----|
| Lock per ChatState | asyncio.Lock | No lock (trust single-thread) | yield points in inject/interrupt create real races; lock is zero-cost when uncontested |
| WAITING_MEDIA phase | Explicit phase | Timer polls transcription count | Explicit = impossible to forget the check |
| STOPPING phase | Explicit phase | cancel_requested flag only | Prevents new inject() during wind-down |
| PendingEntry dataclass | Structured | Raw dict | Type safety, slots for memory |
| ChatRegistry | Separate class | Global dict + function | Encapsulates session creation + shared config |
| Handlers in one file | handlers.py | handlers_commands.py + handlers_messages.py | Not enough volume to justify split (~300 lines total) |
| Tests | Manual first, pytest later | pytest from day 1 | Refactor is already risky; adding test infra doubles scope |

## Appendix A: Edge Case Semantics (from Codex review)

### /clear exact scope
Rejected during PROCESSING/COMPACTING/STOPPING. When allowed (IDLE/COLLECTING/WAITING_MEDIA):
1. Under lock: cancel `_debounce_task`, clear `pending`, clear `deferred`, reset `cancel_requested=False`, `compact_requested=False`, `pending_model=None`, clear `batch_message_ids`, reset `pending_transcriptions=0`, increment `generation`, increment `media_generation`, set `phase=ChatPhase.IDLE`.
2. Release lock.
3. `await session.reset_async()` — I/O happens outside lock.
4. No need for CLEARING phase since /clear is rejected during active phases.

### Stale transcription after /clear
`transcription_finished(entry, generation)` checks `generation == self.generation`. If mismatch — log and discard the entry. Prevents re-enqueueing work from a cleared session.

### /model during PROCESSING
Deferred acceptance: acknowledge "model change applies after current response", store as `pending_model = (model_id, use_1m)` on ChatState. Applied in `_finish_processing()` before draining deferred. User sees confirmation, not rejection.

### /debounce semantics
Per-chat `self.debounce_sec`. `set_debounce(n)` updates immediately. Already-armed timer runs with old value (not restarted). Next `accept_entry` uses new value.

### /compact during PROCESSING
Sets `compact_requested=True`. Compaction runs after response completes (in `_maybe_auto_compact`). Not rejected, not run mid-stream.

### Shutdown cleanup
`ChatRegistry.shutdown()`: for each ChatState — cancel debounce_task, cancel any processing_task if applicable, log. Called in `main()` finally block or `dp.shutdown` callback.

### Urgent reminders during PROCESSING
`run_urgent_prompt(prompt)` creates `PendingEntry(prompt=prompt, message_id=0, message=None, source="reminder", reply_target=chat_id)`. If PROCESSING → try inject. If inject fails → queue to deferred. If IDLE → start new turn via `_start_processing`. `_run_batch` handles `entry.message is None` by using `bot.send_message(chat_id, ...)` directly instead of `message.answer(...)`.

### WAITING_MEDIA timeout
`TRANSCRIPTION_WAIT_MAX = 30` seconds. If `_on_debounce_elapsed` enters WAITING_MEDIA, it arms a 30s deadline. If deadline fires and `pending_transcriptions > 0`: log warning, increment `media_generation` (separate from `generation`), set `pending_transcriptions = 0`, process available entries. Late `transcription_finished` callbacks compare `media_generation` — mismatch = discard always. `media_generation` is a ChatState field (int, starts 0), incremented on timeout AND on `/clear`.

### Lock boundary specification
The `_lock` is held **only for state transitions** — reading/mutating `phase`, `pending`, `deferred`, `cancel_requested`, `compact_requested`, `generation`, `pending_model`. Lock is **never held** across:
- `_run_batch()` / `_ask()` (long I/O, streaming)
- `session.inject()` / `session.interrupt()` (SDK calls)
- `compact_session()` (network I/O)
- `bot.send_message()` / `bot.edit_message_text()` (Telegram API)

Pattern: acquire lock → snapshot batch + update phase → release lock → do I/O → acquire lock → finalize phase + drain deferred → release lock.

### Shutdown cleanup
`ChatRegistry.shutdown()`:
```python
async def shutdown(self):
    for chat in self._chats.values():
        if chat._debounce_task and not chat._debounce_task.done():
            chat._debounce_task.cancel()
        if chat._processing_task and not chat._processing_task.done():
            chat._processing_task.cancel()
    self._chats.clear()
```
Called from `main()` finally block or `dp.shutdown` callback. Prevents orphaned tasks on restart.
