# Refactoring Plan: kesha-tg-bot (Claude Opus)

## 1. Target Architecture

```
kesha-tg-bot/
  bot.py              # ~120 lines: main(), dp setup, startup/shutdown, entrypoint
  config.py           # ~80 lines: env loading, constants, STRINGS, t()
  chat_state.py       # ~200 lines: ChatState class (the state machine)
  pipeline.py         # ~300 lines: enqueue(), _debounce_fire(), _process_batch(), _ask()
  media.py            # ~180 lines: download, transcribe, cache, cleanup, media_name
  handlers.py         # ~250 lines: all @dp.message handlers
  telegram_utils.py   # ~80 lines: _send_safe(), split_msg(), typing_loop(), extract_*_with_urls()
  claude_session.py   # unchanged
  tool_status.py      # unchanged
  compact.py          # unchanged
  kesha_tools.py      # minor changes (use ChatState instead of _bot_ref._processing)
  reminders.py        # unchanged
```

Key principle: **one ChatState object per chat_id holds all mutable per-chat state**. Every function that currently checks/modifies globals instead reads/writes a single `ChatState` instance.

## 2. ChatState Class

```python
# chat_state.py

import asyncio
from enum import Enum, auto
from typing import Optional


class Phase(Enum):
    IDLE = auto()
    DEBOUNCING = auto()
    PROCESSING = auto()
    COMPACTING = auto()


class ChatState:
    """Per-chat state machine. All mutable per-chat state lives here."""

    def __init__(self, chat_id: int):
        self.chat_id: int = chat_id
        self.phase: Phase = Phase.IDLE

        # Debounce buffer
        self.pending: list[dict] = []
        self.pending_timer: Optional[asyncio.Task] = None

        # Processing state
        self.cancel_requested: bool = False
        self.batch_message_ids: list[int] = []
        self.last_user_message_id: Optional[int] = None

        # Queue for messages arriving during processing/compaction
        self.queued_batches: list[list[dict]] = []

        # Transcription tracking
        self.pending_transcriptions: int = 0

    @property
    def is_idle(self) -> bool:
        return self.phase == Phase.IDLE

    @property
    def is_processing(self) -> bool:
        return self.phase == Phase.PROCESSING

    @property
    def is_compacting(self) -> bool:
        return self.phase == Phase.COMPACTING

    def start_debounce(self):
        if self.phase == Phase.IDLE:
            self.phase = Phase.DEBOUNCING

    def start_processing(self, batch: list[dict]):
        self.phase = Phase.PROCESSING
        self.cancel_requested = False
        self.batch_message_ids = [e["msg"].message_id for e in batch]

    def start_compacting(self):
        self.phase = Phase.COMPACTING

    def finish_compacting(self):
        self.phase = Phase.IDLE

    def finish_processing(self):
        self.phase = Phase.IDLE
        self.cancel_requested = False
        self.batch_message_ids = []

    def drain_queue(self) -> list[dict]:
        merged = []
        for b in self.queued_batches:
            merged.extend(b)
        self.queued_batches.clear()
        merged.extend(self.pending)
        self.pending.clear()
        if self.pending_timer and not self.pending_timer.done():
            self.pending_timer.cancel()
        self.pending_timer = None
        return merged

    def queue_batch(self, batch: list[dict]):
        self.queued_batches.append(batch)


_chats: dict[int, ChatState] = {}

def get_chat(chat_id: int) -> ChatState:
    if chat_id not in _chats:
        _chats[chat_id] = ChatState(chat_id)
    return _chats[chat_id]
```

**State transitions:**
```
IDLE --(message)--> DEBOUNCING --(timer)--> PROCESSING --(done)--> IDLE
                                                |
                                                v
                                           COMPACTING --(done)--> IDLE

During PROCESSING: inject via session.inject(), or queue on failure
During COMPACTING: always queue
/clear during PROCESSING: rejected
/stop during PROCESSING: cancel_requested = True + interrupt()
```

## 3. File Breakdown

| New file | Source lines from bot.py | Size |
|----------|------------------------|------|
| config.py | 1-42, 43-44, 46-79, 83-198, 200-209, 1137-1143 | ~80 lines |
| media.py | 214-260, 231-272, 247-256, 424-503, 534-537 | ~180 lines |
| telegram_utils.py | 335-412, 415-420, 539-552, 734-759, 762-775 | ~80 lines |
| chat_state.py | New file | ~200 lines |
| pipeline.py | 567-568, 574-605, 608-696, 701-731, 779-968 | ~300 lines |
| handlers.py | 971-997, 1000-1033, 1038-1387 | ~250 lines |
| bot.py | 276-277, 278-296, 298-314, 1389-1509 | ~120 lines |

## 4. Migration Strategy

### Phase 1: Extract ChatState (~1 hour)

Highest value. Replaces all globals in-place inside bot.py, no file moves.

1. Create `chat_state.py` with ChatState + get_chat()
2. `from chat_state import get_chat`
3. Delete 8 global dicts/sets
4. Replace every access:
   - `_processing.add(chat_id)` → `get_chat(chat_id).start_processing(batch)`
   - `chat_id in _processing` → `get_chat(chat_id).is_processing`
   - `_compacting.add(chat_id)` → `get_chat(chat_id).start_compacting()`
   - `_pending[chat_id].append(...)` → `get_chat(chat_id).pending.append(...)`
   - `_cancel.add(cid)` → `get_chat(cid).cancel_requested = True`
   - etc. (~50 access points)
5. Update kesha_tools.py `compact_context`: `get_chat(chat_id).is_processing`

**Verification:** `python -c "import bot"` + manual test all message types + grep for old globals = 0.

### Phase 2: Extract utility modules (~1.5 hours)

Mechanical extraction, each step independently deployable.

- 2a: config.py (constants, i18n, logging)
- 2b: telegram_utils.py (pure functions)
- 2c: media.py (download, transcribe, cache, cleanup)

Bot object reference: use `set_bot()` pattern (already exists in kesha_tools).

### Phase 3: Extract pipeline + handlers (~1 hour)

- 3a: pipeline.py (enqueue, debounce, process_batch, _ask)
- 3b: handlers.py with `register(dp, bot)` function
- 3c: Slim bot.py to ~120 lines entrypoint

## 5. Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Missing a state access (~50 points) | `grep` for old globals = 0 hits |
| Queue drain behavior change | Line-by-line comparison of drain_queue() vs old finally block |
| Circular imports in Phase 2 | Create bot in config.py or use set_bot() late binding |
| Handler registration order | Preserve exact order (media_group before photo, text before fallback) |
| Hot deployment crash | `python -c "import bot"` before every `systemctl restart` |

## 6. What NOT to Change

- **claude_session.py** — clean, single-responsibility
- **tool_status.py** — self-contained tracker class
- **compact.py** — pure functions with clean interface
- **reminders.py** — ReminderDB + scheduler, clean injection via callback
- **The streaming/draft approach in _ask** — complex but correct, just move as-is
- **The debounce+inject architecture** — sound design, bugs were in state tracking not flow
- **system_prompt.txt** — content, not code

## Effort Summary

| Phase | What | Time | Deploys |
|-------|------|------|---------|
| 1 | ChatState, replace globals | 1h | Yes |
| 2 | config.py, telegram_utils.py, media.py | 1.5h | Yes (each step) |
| 3 | pipeline.py, handlers.py, slim bot.py | 1h | Yes |
| **Total** | | **3.5h** | |

**Phase 1 alone fixes all race conditions.** Phases 2-3 are for readability.
