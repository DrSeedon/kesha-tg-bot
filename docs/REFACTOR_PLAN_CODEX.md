# Refactoring Plan for Kesha Telegram Bot

## 1. Target Architecture

The target is not “microservices” or a generic framework. For a 2-user bot, the right shape is:

- `bot.py` becomes bootstrap only: config, logging, aiogram wiring, startup tasks.
- All per-chat mutable runtime moves into one `ChatState` object per `chat_id`.
- Telegram handlers become thin adapters: parse message/media, build a `PendingEntry`, call `ChatRegistry.get(chat_id).accept_entry(...)`.
- `ChatState` owns the full lifecycle: debounce, waiting for transcription, active Claude turn, stop, compaction, deferred messages, urgent reminders.
- Existing cohesive modules stay mostly intact: `claude_session.py`, `tool_status.py`, `compact.py`, `reminders.py`.
- `kesha_tools.py` stops reaching into `bot.py` globals directly; it uses an injected app/runtime object.

The key rule after refactor:

- Only `ChatState` mutates per-chat runtime.
- Handlers, reminders, and MCP tools never mutate `_processing`, `_queue`, `_cancel`, etc. because those globals no longer exist.

## 2. `ChatState` Design

### Core types

```python
from dataclasses import dataclass, field
from enum import StrEnum
from collections import deque
from typing import Literal, Optional

class ChatPhase(StrEnum):
    IDLE = "idle"
    COLLECTING = "collecting"
    WAITING_MEDIA = "waiting_media"
    PROCESSING = "processing"
    STOPPING = "stopping"
    COMPACTING = "compacting"

@dataclass(slots=True)
class PendingEntry:
    message: types.Message
    prompt: str
    source: Literal["user", "reminder", "system"]
    message_id: int
    created_at: float

@dataclass(slots=True)
class ChatSnapshot:
    chat_id: int
    phase: ChatPhase
    pending_count: int
    deferred_batches: int
    pending_transcriptions: int
    cancel_requested: bool
    session_id: str | None
    model: str
```

### Main class interface

```python
class ChatState:
    def __init__(
        self,
        chat_id: int,
        bot: Bot,
        session: ClaudeSession,
        debounce_sec: int,
        auto_compact_pct: float,
        reminder_service: "ReminderFacade",
    ) -> None: ...

    async def accept_entry(self, entry: PendingEntry) -> None: ...
    async def transcription_started(self, source_message_id: int) -> None: ...
    async def transcription_finished(self, source_message_id: int, entry: PendingEntry | None = None) -> None: ...
    async def request_stop(self) -> bool: ...
    async def request_clear(self) -> bool: ...
    async def request_compact(self, reason: Literal["manual", "auto"]) -> bool: ...
    async def run_urgent_prompt(self, prompt: str) -> None: ...
    async def get_snapshot(self) -> ChatSnapshot: ...

    async def set_model(self, model: str, use_1m: bool) -> None: ...
    async def set_debounce(self, seconds: int) -> None: ...
```

### Internal responsibilities

```python
class ChatState:
    phase: ChatPhase
    pending_entries: list[PendingEntry]
    deferred_batches: deque[list[PendingEntry]]
    pending_transcriptions: int
    cancel_requested: bool
    compact_requested: bool

    debounce_task: asyncio.Task | None
    processing_task: asyncio.Task | None
    lock: asyncio.Lock

    async def _arm_debounce_locked(self) -> None: ...
    async def _on_debounce_elapsed(self) -> None: ...
    async def _start_processing_locked(self, batch: list[PendingEntry]) -> None: ...
    async def _run_batch(self, batch: list[PendingEntry]) -> None: ...
    async def _maybe_run_compaction(self) -> None: ...
    async def _drain_deferred_locked(self) -> None: ...
    async def _try_inject_into_running_turn_locked(self, batch: list[PendingEntry]) -> bool: ...
```

### State transitions

| From | Event | To | Action |
|---|---|---|---|
| `IDLE` | new user/media entry | `COLLECTING` | append entry, arm debounce |
| `COLLECTING` | more entries | `COLLECTING` | append, reset debounce |
| `COLLECTING` | debounce elapsed, no pending transcription | `PROCESSING` | build batch, start Claude turn |
| `COLLECTING` | debounce elapsed, transcription pending | `WAITING_MEDIA` | wait until pending transcription count reaches 0 |
| `WAITING_MEDIA` | all transcriptions finished | `PROCESSING` | process accumulated batch |
| `PROCESSING` | new entry arrives | `PROCESSING` | try `session.inject`; on failure push to `deferred_batches` |
| `PROCESSING` | `/stop` | `STOPPING` | set `cancel_requested`, call `session.interrupt()` once |
| `STOPPING` | Claude stream ends | `IDLE` or `COMPACTING` | finalize response, then drain deferred or compact |
| `PROCESSING` | auto/manual compact requested | `PROCESSING` | set `compact_requested=True`; do not compact mid-stream |
| `IDLE` | `/compact` | `COMPACTING` | run `compact_session()` immediately |
| `COMPACTING` | entry/reminder arrives | `COMPACTING` | queue into `deferred_batches` |
| `COMPACTING` | compaction finishes | `IDLE` or `PROCESSING` | drain deferred immediately |
| `IDLE` or `COLLECTING` | `/clear` | `IDLE` | cancel debounce, clear pending, reset session |
| busy state | `/clear` | same state | reject politely; do not half-reset active turn |

### Important design choices

- Use one `asyncio.Lock` per `ChatState`. This is enough here; no need for Redis, actor framework, or DB-backed runtime.
- Keep streaming response rendering outside the lock, but all state transitions happen under the lock.
- `cancel_requested` is state owned by `ChatState`, not a global set.
- Manual and auto compaction use the same path; only the trigger differs.
- Urgent reminders become synthetic entries owned by `ChatState`, not a separate parallel control flow.

## 3. File Breakdown

A practical split, not over-engineered:

| File | Responsibility |
|---|---|
| `bot.py` | bootstrap only: config, logging, bot/dispatcher creation, startup/shutdown wiring |
| `chat_state.py` | `ChatPhase`, `PendingEntry`, `ChatSnapshot`, `ChatState` |
| `chat_registry.py` | `ChatRegistry`, lazy creation of `ChatState` per chat, shared app services |
| `response_stream.py` | current `_ask()` logic: streaming, drafts, `ToolStatusTracker`, retries |
| `message_format.py` | `user_prefix`, `forward_meta`, `reply_meta`, `extract_text_with_urls`, `extract_caption_with_urls`, prompt assembly |
| `media_service.py` | download, cache, transcription, media file naming |
| `telegram_io.py` | `_send_safe`, `typing_loop`, `split_msg`, small Telegram wrappers |
| `handlers_commands.py` | `/start`, `/status`, `/clear`, `/compact`, `/stop`, `/model`, `/debounce`, `/debug`, `/restart`, `/ping` |
| `handlers_messages.py` | text/photo/voice/video/document/audio/album/fallback handlers |
| `claude_session.py` | keep as session adapter, with only minor API cleanup |
| `tool_status.py` | keep |
| `compact.py` | keep |
| `reminders.py` | keep storage/scheduler logic; call `ChatRegistry` instead of bot globals |
| `kesha_tools.py` | keep tool surface, replace `_bot_ref` global coupling with injected runtime |

## 4. Migration Strategy

### Phase 0: Stabilization hotfixes
1. Land two small fixes before the refactor: guard `/ping` with `allowed(...)`, and remove `_resolve_chat()` fallback-to-first-allowed behavior so tools cannot route to the wrong chat. This is independently deployable and reduces current security exposure immediately.
2. Add characterization tests around current behavior before moving code: debounce batching, streaming finalization, stop, manual compact, urgent reminder injection, media transcription wait. This phase changes little code and is safe to ship first.

### Phase 1: Extract without changing behavior
1. Move pure helpers and side-effect helpers out of `bot.py`: message formatting, media/transcription/cache, Telegram send helpers, streaming responder. Keep existing globals for now. This is deployable because behavior stays the same and only import paths change.
2. Add a thin `ChatRegistry.get_session(chat_id)` wrapper, but still back it with existing `_sessions`. This creates the seam for the next phase without changing runtime logic.
3. Keep all handlers registered exactly as now so rollback is trivial.

### Phase 2: Introduce `ChatState` as the single state owner
1. Create `ChatState` and `ChatRegistry`, then move `_pending`, `_pending_timers`, `_processing`, `_cancel`, `_queue`, `_compacting`, `_pending_transcriptions`, `last_user_message_id`, and `current_batch_message_ids` into fields on `ChatState`.
2. Rewrite `enqueue`, `/stop`, `/clear`, `/compact`, and urgent reminder entrypoints to call `ChatState` methods instead of touching globals. This is the real bug-reduction phase and should still be deployable because external bot behavior remains the same.
3. Keep `response_stream.py` using the same Claude SDK and Telegram draft/status behavior. Do not redesign the user-facing streaming UX in this phase.

### Phase 3: Unify reminders/tools/startup around `ChatRegistry`
1. Change `reminders.py` to call `registry.get(chat_id).run_urgent_prompt(...)` instead of its current side path plus `session._is_processing` probing. This eliminates split orchestration.
2. Change `kesha_tools.py` to use injected runtime services instead of `_bot_ref` module reach-through. Expose explicit interfaces like `runtime.get_chat(chat_id)`, `runtime.get_status(chat_id)`, `runtime.set_model(chat_id, ...)`.
3. Auto-compaction becomes an internal `ChatState` post-processing decision. Manual compaction goes through the same state machine. Deployable because features stay intact; only ownership changes.

### Phase 4: Cleanup and hardening
1. Delete dead compatibility code and all removed globals from `bot.py`.
2. Add snapshot-based `/status` output from `ChatState.get_snapshot()`.
3. Add guardrails in `ClaudeSession`: public `is_processing` property, no external callers poking `_is_processing`. This is a safe cleanup phase and can ship independently after behavior is proven stable.

## 5. Risk Assessment and Testing Strategy

### Main risks
- The highest risk is changing async timing while preserving current user-visible behavior.
- The second risk is double-processing or dropped messages during the cutover from globals to `ChatState`.
- The third risk is reminder/tool routing regressions because those paths currently bypass the main message flow.

### Testing strategy
- Use `pytest` + `pytest-asyncio`.
- Create a fake `ClaudeSession` that can emit scripted chunks: `text_delta`, `text`, `tool`, `turn_done`, `error`.
- Create a fake bot transport that records `send_message`, `edit_message_text`, `SendMessageDraft`, `send_chat_action`, and reactions.

### Minimum test matrix
- Three text messages inside debounce become one batch.
- Voice/video-note transcription delays the batch until transcription finishes.
- Message arrives during `PROCESSING` and successful `inject()` keeps one turn.
- Message arrives during `PROCESSING` and failed `inject()` is deferred and processed after the turn.
- `/stop` during streaming calls `interrupt()` once and transitions `PROCESSING -> STOPPING -> IDLE`.
- Manual `/compact` only runs from `IDLE`/safe point and drains queued messages after finish.
- Auto-compaction after a turn does not lose messages arriving during compaction.
- Urgent reminder injects during active processing, otherwise creates a synthetic turn.
- Two chats running concurrently do not share state, drafts, reminders, or tool routing.
- Unauthorized users cannot access `/ping` or any command path.
- MCP tool actions require explicit chat context and never fall back to another allowed user.

### Rollout safety
- Keep one feature flag during Phase 2: `USE_CHAT_STATE=true/false`. In off mode, keep old globals. Remove the flag only after the tests above pass and one production deploy is stable.
- Add structured logs for transitions: `chat_id`, `from_phase`, `event`, `to_phase`, `pending`, `deferred`, `transcriptions`. This will make any new race visible immediately.

## 6. What Not to Change

- Do not replace `ClaudeSession` persistence/resume behavior. It is one of the stable parts.
- Do not rewrite `ToolStatusTracker` UX. The status bubble logic is already well-contained.
- Do not redesign compaction prompts or summary semantics in `compact.py`.
- Do not change reminder schema, recurrence math, or lazy reminder concept unless a bug forces it.
- Do not change Deepgram integration shape, media cache files, or file naming rules during this refactor.
- Do not introduce external infrastructure like Redis, Celery, or a DB-backed job queue. This bot does not need it.
- Do not turn this into a generic framework. Keep it optimized for one bot, two users, and fast maintenance.

## 7. Practical Scope for 2–3 Sessions

1. Session 1: Phase 0 + Phase 1. Extract helpers, add tests, land the two security fixes.
2. Session 2: Phase 2. Introduce `ChatState` and move all per-chat runtime into it.
3. Session 3: Phase 3 + Phase 4. Rewire reminders/tools/startup, remove compatibility globals, harden APIs.

If time is tighter, Session 1 and Session 2 can be merged, but I would not merge Phase 2 and Phase 3 on the first attempt. The biggest value comes from centralizing state first; reminders/tools cleanup can follow once the main message path is stable.
