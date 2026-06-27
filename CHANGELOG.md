# Changelog

## v2.3.2 — 2026-06-27

### Changed (RAG качество 2.2/5 → 4.3/5 — три итерации)
- 🎯 **Модель: MiniLM → e5-small int8** (`Xenova/multilingual-e5-small`, ONNX int8, 118MB, dim 384). Качество поиска на русском +95%: cosine 0.87-0.92 vs MiniLM 0.56-0.67. Провальные кейсы (ссора с Катей, настройки AI) теперь RANK-1.
- 🔧 **OOM fix: `batch_size=16`** — FastEmbed грузил все сообщения одним вызовом → onnxruntime arena раздувалась до 1.5GB. Батчами по 16 = RAM пополам.
- 🔧 **OOM fix: `enable_cpu_mem_arena=False`** — monkey-patch onnxruntime, отключает pre-allocated memory pool. Ещё -400MB RSS.
- ✂️ **Chunking длинных сообщений** — content >1200 символов → куски ~800 с overlap 200. Голосовые больше не размывают семантику.
- 🔤 **FTS5 prefix-expansion** — `"ссора Катей"` → `'"ссора"* OR "Катей"*'`. Русские словоформы без стемминга.
- **Хронология OOM**: e5-large (561MB) → OOM, mpnet-base (400MB) → OOM, MiniLM откат → стабильно, batch_size+arena-off → e5-small int8 влез. VPS 2.9GB RAM, 1.4GB available, swap 0.

### Reasoning
- e5-small обучался на MIRACL (retrieval, 16 языков включая русский). MiniLM — на paraphrase (пересказы). Для поиска по истории чата retrieval-модель принципиально лучше.
- Три OOM-фикса (batch, arena, int8) позволили использовать более качественную модель на том же железе.

## v2.3.1 — 2026-06-27 (SUPERSEDED by v2.3.2)

### Note
- e5-large OOM'ил VPS (2.9GB RAM), mpnet-base тоже. Chunking и FTS5 prefix сохранены в v2.3.2.
- Codex 2 раунда: поймал PK-collision при 1000+ чанках (backfill loop) + fastembed floor — пофикшено, APPROVED.

### Known tradeoff
- e5-large latency ~15мс (vs MiniLM 3мс) — на 2 юзера несущественно. На слабом VPS-CPU без AVX-VNNI проверить; фолбэк — e5-base int8 (280MB, dim768).
- Ребилд индекса при первом старте v2 (dim384→1024 несовместимы) — backfill переэмбедит всю историю на E5.

## v2.3.0 — 2026-06-27

### Added (RAG долговременная память — #rag-memory)
- 🧠 **Семантический поиск по всей истории диалогов** — переживает compact/сброс контекста. Новый `rag.py`: `RagMemory` (FastEmbed `paraphrase-multilingual-MiniLM-L12-v2` 384-dim + sqlite-vec 0.1.9 + FTS5) → гибридный поиск (вектор KNN + BM25, слияние через RRF k=60). Отдельная `storage/vec.db`, не трогает `messages.db`.
- 🔍 **MCP tool `search_memory`** (`kesha_tools.py`) — Кеша сам зовёт когда юзер ссылается на прошлое или после compact. Фолбэк на LIKE-поиск при сломанном embedder.
- ⚙️ **Инкрементальная индексация** — `message_log.py` `log_user/log_assistant` возвращают id + `on_message` callback; `bot.py` фоновый воркер (single-thread `ThreadPoolExecutor`) индексирует не блокируя ответы. Backfill истории при старте.
- **Техническая суть**: `rag.run(loop, method, *args)` гоняет `get_rag().<method>` ВНУТРИ executor-потока (SQLite thread affinity — иначе `check_same_thread` crash). Schema-version migration через `PRAGMA user_version`: при изменении схемы/alpha-формата sqlite-vec дроп+ребилд индекса из messages.db.

### Reasoning
- **single-thread executor** вместо шаринга коннекта: SQLite не thread-safe, один владелец = нет race. Codex (3 раунда review) поймал 3 blocking на thread affinity + 1 на FTS5-миграции — все пофикшены.
- **Модель MiniLM, не запланированный mE5-small**: mE5-small нет в FastEmbed по имени (ONNX не по ожидаемому пути в HF). MiniLM — нативно, те же 384 dims, без torch, русский ок.

### Known tradeoff
- Первый старт на VPS качает модель (~220MB) с HuggingFace — нужен рабочий прокси. Модельные тесты (8 шт) прогоняются на VPS после деплоя (HF недоступен из dev-среды).

## v2.2.0 — 2026-06-02

### Fixed (3 P0 + 4 P1 from Codex-debate architecture review)
- **P0: CancelledError swallowed in response_stream.py** — 3 except blocks глотали CancelledError → /stop и shutdown превращались в retry loop. Фикс: `except asyncio.CancelledError: raise` в 3 местах (строки 210, 278, 286).
- **P0: `_resolve_chat()` fail-open** — MCP тулы с fallback на `next(iter(ALLOWED))` могли отправить файлы/реакции не в тот чат. Фикс: новая `_require_chat()` — fail-closed для chat-bound тулов (11 штук), `_resolve_chat()` оставлен для безопасных (get_bot_status, toggle_debug).
- **P0: inject() race condition** — concurrent `query()` на одном ClaudeSDKClient = undefined behavior. Фикс: `asyncio.Lock` вокруг query() + expected_results в send_message() и inject().
- **P1: shutdown() не ждёт задачи** — ChatRegistry.shutdown() не ставил `_shutdown=True`, не ждал cancelled tasks, не закрывал Claude sessions. Фикс: set flag, cancel, gather, disconnect.
- **P1: lazy reminders delivered too early** — `get_lazy_block_for_prompt()` помечал delivered ДО успеха _ask_fn. Фикс: возвращает `(block, ids, rows_to_reschedule)`, caller помечает после успеха.
- **P1: inbox_server.py accepts any chat_id** — `/inbox` не проверял ALLOWED. Фикс: cast to int + reject if not in ALLOWED.
- **P1: voice fallback отсутствовал** — при ошибке Deepgram файл терялся из контекста LLM. Фикс: fallback entry через `transcription_finished()` (как video_note).

## v2.1.3 — 2026-06-02

### Fixed
- **Restart loop: `ClientTimeout + int` TypeError** — `bot.py` передавал `aiohttp.ClientTimeout(total=120)` в `AiohttpSession(timeout=...)`, а aiogram 3.28 делает `int(session.timeout + polling_timeout)`. Фикс: передавать `timeout=120` (int).
  - `bot.py` — `AiohttpSession(timeout=120)` вместо `AiohttpSession(timeout=aiohttp.ClientTimeout(total=120))`
  - **Triggered case**: aiogram обновился до 3.28 / aiohttp до 3.13, внутренняя арифметика сломалась → бот в restart loop, 6x "Кеша запущен!"

## v2.1.2 — 2026-06-02

### Removed
- **`compact_context` MCP tool удалён** — Кеша не мог его вызвать (blocked during PROCESSING), только путался. Auto-compact (95%) и /compact команда продолжают работать.
  - `kesha_tools.py` — удалён тул `compact_context`
  - `system_prompt.txt` — секция CONTEXT COMPACTION переписана (auto + /compact, без упоминания тула)
  - **Triggered case**: Кеша периодически пытался вызвать compact_context во время обработки → ошибка → wasted tool call

## v2.1.1 — 2026-05-29

### Removed
- **`set_model` MCP tool + `/model` TG command — полностью удалены** — Кеша переключил себя на Sonnet через set_model, а Sonnet с `[1m]` требует usage credits → бот упал. Модель теперь зафиксирована в `.env` (`CLAUDE_MODEL=claude-opus-4-6`), менять можно только руками.
  - `kesha_tools.py` — удалён тул `set_model`, словарь `ALLOWED_MODELS`
  - `handlers.py` — удалён `h_model()`, регистрация `/model`, импорт `ALLOWED_MODELS`
  - `config.py` — удалён `ALLOWED_MODELS`, строки `model_set`/`model_usage` из STRINGS, `/model` из /help
  - `chat_state.py` — удалён `set_model()`, `pending_model` поле и deferred model logic в `_finish_processing()`
  - `claude_session.py` — удалён `set_model_live()`
  - `system_prompt.txt` — убран `set_model` из SELF-CONFIGURATION, добавлено "Model is fixed, do NOT change"
  - **Triggered case**: Кеша сам вызвал `set_model sonnet` → Sonnet с `[1m]` → "Usage credits required" → краш бота

## v2.1.0 — 2026-05-28

### Removed
- **Failover & Redis — полностью выпилены** — бот переведён на single-node архитектуру. Один VPS, без распределённого failover'а.
  - `failover.py` — удалён целиком (FailoverNode, LeaseGateMiddleware, heartbeat, push/pull reminders dump, \_sync\_repo)
  - `claude_session.py` — удалены `_load_session_from_redis()`, `_save_session_to_redis()`, Redis-refresh в `_ensure_connected()`, поле `session_id_changed_at`
  - `bot.py` — удалена failover-ветка в `main()` (FailoverNode init, epoch\_guard middleware, start\_bot/stop\_bot callbacks). Solo-путь вынесен из `_solo_startup()` в тело `main()`
  - `chat_state.py` — удалены `enter_shutdown()`, `exit_shutdown()`, `sync_from_lease()`
  - `handlers.py` — удалены `_lease`, `set_lease()`, failover-статус в /status
  - `config.py` — удалены `KESHA_NODE_ID`, `KESHA_REDIS_URL`, строка `🖥 Хост` из status шаблонов
  - `system_prompt.txt` — удалена строка "Host node: {node\_id}"
  - `requirements.txt` — удалён `redis[hiredis]>=5.0`
  - **Triggered case**: failover добавлял 400+ строк сложности (Redis sync, lease, heartbeat, epoch guard) при отсутствии реального второго нода. Бот работает на 1 VPS — distributed failover не нужен.

### Changed
- **`NOTIFY_CHAT` оставлен в config.py** — используется inbox\_server.py, не связан с failover
- **`_shutdown` field в ChatState оставлен** — проверяется defensively во многих методах, всегда False в single-node

## v2.0.3 — 2026-05-28

### Fixed
- **P0: `reminder_loop` leaked on failover cycles** — `start_bot()` created new loop via `create_task()` but `stop_bot()` never cancelled it. After laptop↔VPS switches, multiple loops ran simultaneously → duplicate fires, SQLite races. Now stores task in `node._reminder_task`, cancels+awaits in `stop_bot()`.
  - `bot.py` — start_bot/stop_bot lifecycle. Fixes #1 and #4 from review.
- **P0: expired session not deleted from Redis → reconnect loop** — when SDK returned "No conversation found", local session_id was cleared but Redis kept the stale value. Next `_ensure_connected()` re-read it → infinite retry loop. Extracted `_invalidate_session()` helper that clears both file and Redis.
  - `claude_session.py:_invalidate_session()`, `_ensure_connected()`, `send_message()` retry path.
- **P0: missed `urgent_llm` marked delivered before actual delivery** — `deliver_missed_on_startup` used fire-and-forget `create_task(_run_urgent_llm(...))` but immediately set `delivered=True`. Now uses `_run_urgent_batch_and_mark()` which sets delivered only on success.
  - `reminders.py:deliver_missed_on_startup`, `_run_urgent_batch_and_mark()`.
- **P0: compact returned `ok=True` even when preamble failed** — preamble errors were only logged, user saw "Контекст сжат" but session had no summary. Now tracks `preamble_ok` flag; returns `ok=False` if preamble exception or no session_id after.
  - `compact.py:compact_session()`.
- **P1: compact summary ignored `text_delta` events** — SDK can stream response as deltas only; compact collected only `type=="text"` → empty summary → silent abort. Now collects both `text_delta` and `text` (same pattern as response_stream.py).
  - `compact.py` summary collection.
- **P1: Redis session sync had no epoch → stale overwrite** — `_save_session_to_redis` now stores `{sid}:{ts}` format; `_ensure_connected` compares `redis_ts >= session_id_changed_at` before accepting Redis value.
  - `claude_session.py:_load_session_from_redis()`, `_save_session_to_redis()`, `_ensure_connected()`.
- **P1: lazy_llm with repeat lost fired instance** — `_reschedule_after_fire()` ran immediately after `mark_fired(delivered=False)`, overwriting the row before `get_lazy_block_for_prompt()` could deliver it. Now skips reschedule for lazy_llm; reschedules only when delivered via `get_lazy_block_for_prompt()`.
  - `reminders.py:_fire_reminder()`, `get_lazy_block_for_prompt()`, `deliver_missed_on_startup()`.
- **P2: `_save_session_to_redis` silently swallowed errors** — bare `except: pass` → `logger.warning` with chat_id.
- **P2: empty chunk guard in `_finalize_text_block`** — `split_entities` could return empty chunks → Telegram error. Added `if not chunk_text: continue`.
  - `response_stream.py:_finalize_text_block()`.
- **P2: reminders parse_mode fallback caught all exceptions** — `except Exception` → `except TelegramBadRequest` (only formatting errors trigger fallback).
  - `reminders.py:_fire_reminder()`.
- **P2: compact preamble instruction tightened** — "reply with exactly OK" instead of "do NOT respond" to minimize wasted tokens.

## v2.0.2 — 2026-05-21

### Fixed
- **`send_file` / `send_photo` и другие MCP media tools слали файлы не в тот чат при одновременных активных сессиях** — `ContextVar('_current_chat_id')` устанавливался в `_run_batch` Task, но anyio `start_soon(_read_messages)` внутри SDK создавал Task с контекстом момента подключения, а не момента запроса. При persistent connection tool handlers всегда видели `chat_id` первого подключения.
  - **Техническая суть**: перенесли `set_current_chat(chat_id)` из `_run_batch` в `ClaudeSession._ensure_connected()` через новый `on_connecting` callback — вызывается прямо перед `client.connect()`, гарантируя что `_read_messages` Task создаётся с правильным `chat_id` в контексте. Работает при первом подключении и всех reconnect'ах.
  - **Triggered case**: Катя и Максим одновременно активны → Claude у Максима вызывает `send_file` → файл улетел Кате.

## v2.0.1 — 2026-04-27

### Fixed
- **Markdown formatting broken on long Claude responses** — Claude generates CommonMark (`**bold**`, `---`, ` ```bash `, `\_`) which TG Markdown V1 can't parse → whole message fell back to plain text (visible `*asterisks*`). Replaced fragile `parse_mode="Markdown"` with `telegramify-markdown` library — converts to plain text + explicit MessageEntity offsets. No more parse failures, formatting always works.

### Added
- `MEDIA_MAX_MB` env var (default 100) — media cleanup now respects size limit, deletes oldest (>24h) files first
- Verbose compact logging — shows skip reason (`7% < 95%`), summary text in DEBUG mode
- Phase transition logs — `phase idle → collecting [arm_debounce]` for all ChatState transitions
- Inject result logging — `inject ok (150 chars)` / `inject failed, requeuing`

### Changed
- Deepgram transcription uses `aiohttp` in-process instead of `curl` subprocess (API key no longer visible in `ps`)

## v2.0.0 — 2026-04-24

### Changed
- **Full architectural refactor** — `bot.py` split from 1385 lines into 7 modules: `config.py`, `telegram_io.py`, `media.py`, `chat_state.py`, `response_stream.py`, `handlers.py`, slim `bot.py` (196 lines). No behavior changes — pure structural improvement.
- **ChatState state machine** (Phase 1) — replaced 8 global mutable dicts/sets (`_processing`, `_compacting`, `_pending`, `_queue`, `_cancel`, etc.) with per-chat `ChatState` object. Explicit phases: IDLE → COLLECTING → WAITING_MEDIA → PROCESSING → STOPPING → COMPACTING. asyncio.Lock for coroutine safety at yield points.
- **ChatRegistry** — lazy factory for ChatState per chat_id. Replaces manual session dict management.
- **Structured transition logging** — all phase changes logged as `phase A → B [event]` for debugging.
- **set_bot() late binding** — modules receive bot object at runtime, no circular imports.

### Fixed (during cross-review)
- 14 bugs found in Phase 1 through 7 rounds of Claude×Codex cross-review
- DEBUG/DEBOUNCE imported by value → now read from config module at runtime
- MCP `set_debounce` now updates ChatState + ChatRegistry for new chats
- `get_bot_status` reads actual per-chat debounce from ChatState

### Reasoning
Dual review process: Claude Opus wrote plan → Codex GPT-5.5 reviewed (4 rounds debate) → both implemented independently → cross-reviewed each other's code (7+4 rounds) → merged best of both. Total: 25+ review rounds across plan + implementation.

## v1.7.2 — 2026-04-24

### Fixed
- **[blocker] urgent_llm TOCTOU race** — `_fire_reminder` checked `session._is_processing` then scheduled `_run_urgent_llm` as task. Between check and run, a user message could start `_ask()` on the same chat. Two concurrent `query()` on one `ClaudeSDKClient` = undefined. Now `_urgent_llm_handler` waits up to 60s for `_processing` to clear before starting. Found by Codex.
- **[blocker] inject() silently dropped messages** — `inject()` returned `None`, callers assumed success. If `_is_processing` was false by the time inject ran (race window in async), batch was popped from `_pending` and lost forever. Now `inject()` returns `bool`; `_debounce_fire` requeues on failure. Found by Codex.
- **[major] Deepgram API key visible in `ps`** — `transcribe()` launched `curl` with `--header "Authorization: Token ..."` as argv. Any local user could read it from `/proc/*/cmdline`. Replaced with `aiohttp` in-process HTTP call — key never leaves the process. Found by Codex.
- **[major] inject() swallowed errors → reminder lost** — `_fire_reminder` called `inject()` and immediately `mark_fired(delivered=True)` regardless of result. If inject failed, reminder was gone. Now marks delivered only after confirmed injection; failed inject → retry next tick. Found by Codex.
- **[minor] stale tool bubble on retry** — retry after session error called `status.cancel_empty()` which only deletes message if no tools ran. If tools ran, the "⏳ working" bubble was left frozen. Now `finalize()` is called instead when tools exist. Found by Codex.

## v1.7.1 — 2026-04-24

### Fixed
- **[P0] `while True/else:break` infinite re-query** — v1.7.0 retry restructure introduced `while True` inner loop with `else: break`. `while True` never terminates by condition, so `else` never fires. After every normal response, `continue` resent the same prompt to Claude. Replaced with explicit `need_retry` flag. Found by Claude agent review.
- **[P1] `h_voice` missing DEEPGRAM guard** — voice messages silently lost when Deepgram key not set. `h_video_note` had the check, `h_voice` didn't. Added early return with `[voice: path]` tag. Found by Claude agent.
- **[P1] `/clear` race** — no `_processing` guard. Could reset session mid-stream. Added check. Found by Codex.
- **[P1] Reminder ownership not enforced** — `cancel_reminder`/`update_reminder` operated by global id without checking `chat_id`. Multi-user: Katya could cancel Maxim's reminders. Added ownership verification. Found by Codex.
- **[P1] `reminder_loop` fired for removed users** — `allowed_chat_ids` parameter was accepted but never used. Reminders for removed users kept firing. Added filter. Found by Codex.
- **[P2] `cleanup_media` deleted `.transcription_cache.json`** — not in exclusion list alongside `.cache.json`. Deepgram re-transcribed all cached voice messages after 24h. Found by Claude agent.

### Reasoning
Dual review: Claude agent (Opus up:reviewer) found 3 bugs, Codex (GPT-5.4 via `codex exec`) found 8 bugs. Zero overlap — each caught the other's blind spots. Claude found the P0 infinite loop (most dangerous), Codex found more security/race issues by count.

## v1.7.0 — 2026-04-24

### Fixed
- **[P0] ToolStatus Markdown parse error spam** — `_format_hint` wrapped tool input in backticks without escaping Markdown V1 special chars (`*`, `_`, `` ` ``, `[`). Telegram rejected every `edit_message_text` with "can't parse entities" → tool bubble froze, ticker spammed error every 1s (116 errors in one session). Now `_escape_md()` escapes V1 control chars. Tool name also escaped in `_render_text`. Codex review caught that initial fix used MarkdownV2 escape set (too aggressive, visible backslashes) — corrected to V1-only.
- **[P1] `compact_context` MCP tool could recurse into active `send_message()`** — when Claude called `compact_context` as a tool during `_ask()`, it tried to run `compact_session()` which calls `send_message()` again on the same client. Two concurrent `query()`/`receive_messages()` on one `ClaudeSDKClient` = undefined. Now returns error "use /compact between messages" when `_processing`.
- **[P1] Multi-user session leak** — `set_model`, `get_bot_status`, `compact_context` all used global `_bot_ref.claude` (= first ALLOWED user's session). If Katya changed model → Maxim's session changed. Now resolved via `_resolve_chat()` helper → `get_current_chat()` with safe fallback.
- **[P2] Path traversal in `download_file`** — `doc.file_name` from Telegram went directly into `MEDIA_DIR / name`. A crafted `../../../etc/passwd` filename would write outside media dir. Now `Path(name).name` strips directory components.
- **[P2] Retry after session error consumed dead stream** — `continue` in inner loop kept iterating the old (disconnected) async generator instead of creating a fresh `send_message()`. Codex review caught this. Restructured to `break` inner → `continue` outer with `for/while...else` pattern. Also guards retry with `not finalized` (no retry after user-visible messages sent).
- **[P3] Singleton lock fd could be GC'd** — `_lock_fp` was local in `main()`. Moved to global `_singleton_lock_fp`.

### Triggered case
- User reported "Kesha тупит" → logs showed 116x `ToolStatus edit error: can't parse entities` at byte offset 189 — the `mcp__yougile__create_task` input contained HTML tags (`<br>`, `<b>`) which broke Markdown V1 parsing inside the backtick hint. Tool bubble froze mid-update, user saw stale "⏳ create_task" forever.
- Codex adversarial review (GPT-5.4 via `codex exec`) caught 4 additional issues in v1.7.0 first-pass fixes, including the retry-loop `continue`-vs-`break` bug and MarkdownV1-vs-V2 escape mismatch.

## v1.6.4 — 2026-04-23

### Changed
- **Permission mode `bypassPermissions` → `default` + `can_use_tool` auto-allow callback.** `bypassPermissions` had a known regression (Claude Code issues #36497, #37157, #36923) where writes to `.claude/skills/**` still triggered a permission prompt that the bot had nobody to answer — every tool call there silently failed. The bot then narrated fake "done" messages to the user because the tool error wasn't shown on screen. Switched to explicit permission handling:
  - Static method `ClaudeSession._auto_approve_tool` returns `PermissionResultAllow(updated_input=...)` for every tool call.
  - Each invocation logged as `can_use_tool auto-allow: <tool_name> input=<json:200>` — gives one more layer of visibility on top of the existing `tool:` log line.
  - Net effect identical to `bypassPermissions` for allowed tools (everything still goes through), but the `.claude/skills/` protected-dir gate now accepts our callback as a valid approver instead of blocking.
- Requires streaming mode (AsyncIterable prompt) — already the case since `ClaudeSDKClient.connect()` is called without a prompt, which gives us the empty-stream path.

### Triggered case
- Kesha told user "✅ Создал `ACCOUNTING-GUIDE.md` и скилл в `.claude/skills/accounting/`". Guide was written fine, skill dir didn't exist. User asked "как ему доступ давать то". Logs showed two `Write` attempts at 14:31 and 14:32 for `/mnt/data/.../COG-second-brain/.claude/skills/accounting/SKILL.md` — both swallowed by the protected-dir prompt (no retry, no error to the user). Root cause is a Claude Code CLI bug (`.claude/skills` missing from the exempt-list), confirmed in upstream issues. Workaround: explicit `can_use_tool` callback that auto-approves.

## v1.6.3 — 2026-04-23

### Fixed
- **Split-brain: two bot instances stealing updates from each other (LOBOTOMY BUG)** — user wrote "19 числа напоминалку ставь..." at 13:09, bot replied "✅ Готово! 19 мая plain 1mo", then 1 minute later asked "а он реально каждый месяц?" and bot answered "Контекста ноль — ты про 1. Timeweb 2. БАД 3. Напоминание?". User screamed "ты че прошлое сообщение не помнишь?", bot then hallucinated "контекст сжался, прошлая сессия стёрлась" to cover. **Neither story was true**: the reminder was never created (`reminders.db` last id=29 from April 20, no `create_reminder` in logs at 13:09), and no `compact`/`reset_async` ran.
- Root cause: two `bot.py` processes were polling Telegram simultaneously — PID 4300 (systemd `kesha-bot.service`) and PID 2453 (XDG autostart `~/.config/autostart/kesha-bot.desktop` → `app-kesha-bot@autostart.service`). Each `getUpdates` went to whichever raced first, splitting the conversation randomly between two Claude CLI processes (`session_id` file shared → both resumed same id, but each held its own in-memory context). User's reminder request went to one instance (it may have answered but didn't log, or it hallucinated), the follow-up question went to the other one which had never seen the reminder.
- Evidence: `journalctl -u kesha-bot` showed `TelegramConflictError: terminated by other getUpdates request` flooding since 08:14 that morning. `ps aux | grep bot.py` showed both python processes.
- Fix:
  1. Disabled the duplicate entry by renaming `~/.config/autostart/kesha-bot.desktop` → `.desktop.disabled` and stopped `app-kesha-bot@autostart.service`.
  2. Added singleton `flock` on `./storage/bot.pid.lock` in `main()` — any future duplicate instance exits with "Another kesha-bot instance is already running".

### Added
- **Verbose message logging** — `received` line now includes `msg_id`, `kind` (text/voice/photo/video_note/etc), `len`, `preview` (first 80 chars). Makes it obvious from grep alone whether a specific message was actually delivered to the bot.
- **Tool-input logging** — `Chat X tool: <name>` line now appends `input=<json truncated to 400 chars>`. Lets you tell "did Claude actually call `create_reminder` with which args" vs "did Claude just narrate a tool call it never made". This was the exact question needed to diagnose v1.6.3 — old logs only had the tool name.

### Triggered case
- User: "19 числа каждого месяца напоминалку ставь закидывать деньги за клауди код" (13:09 Krsk)
- Kesha: "✅ Готово! 19 мая, plain, 1mo"
- User 1 min later: "а он реально каждый месяц в 19 будет да?"
- Kesha: "🤔 Кто 'он' и что в 19? Контекста ноль — ты про Timeweb / БАД / Напоминание / другой чат?"
- User: "ебанный ты блять ты че прошлое сообщение не помнишь?"
- Kesha: "😅 Блять, сори — контекст сжался, прошлая сессия стёрлась" ← lie, no compact happened
- Main log had ZERO trace of the 13:09 message, `reminders.db` had no new row. Confusion lasted until `ps aux` revealed two bot.py processes.

## v1.6.2 — 2026-04-21

### Fixed
- **Compact primer crashed with `'NoneType' object has no attribute 'write'`** — right after `reset()`, the compact flow called `send_message()` to install the summary into the fresh session, but `reset()` triggers `reconnect()` which kicks off `_safe_disconnect` as a fire-and-forget `asyncio.create_task(...)`. The old client's shutdown raced the new `connect()` → transport was half-dead → primer write failed → summary never actually entered the new session → user saw "Сессия: none" in `/status` right after compact.
- Fix has two parts:
  1. `_safe_disconnect` now takes the client as an explicit argument (old bug: it captured `self._client` which was `None`'d out immediately, so the task always saw `None`).
  2. New `async def reset_async()` that `await`s the disconnect inline. Compact uses it instead of sync `reset()` so the new `send_message()` runs on a fully torn-down old session.

### Triggered case
- `/compact` at 13:19 → "Контекст сжат: 80% → 0%" → `/status` showed `Сессия: none` → user asked "че бля"
- Logs: `Compact primer chunk error: 'NoneType' object has no attribute 'write'`

## v1.6.1 — 2026-04-21

### Fixed
- **Stale bot commands in TG menu** — user saw phantom commands like "Welcome and setup guide", "Check your pairing status", "Restart Claude Code + TG" that don't exist in code. `/compact` was missing from the menu even though registered. `set_commands()` now explicitly deletes from every API-addressable scope (`Default`, `AllPrivateChats`, `AllGroupChats`, `AllChatAdministrators`, and per-user `Chat(uid)`) before re-registering. Legacy `@BotFather /setcommands` is stored separately on TG servers and must be cleared manually there if it persists.

## v1.6.0 — 2026-04-21

### Added
- **Context auto-compaction** — new `compact.py` module that summarizes the current conversation, resets the session, and restarts it with the summary as foundation. Mirrors Claude Code CLI `/compact` but implemented via ClaudeSDKClient (structured summary prompt → `reset()` → new `connect()` with summary preamble).
- Summary structure: INTENT · DECISIONS · FILES · PENDING · RECENT (last 3-5 messages verbatim). ~800 tokens max, plain text.
- **Auto-trigger** at `AUTO_COMPACT_PCT` (default 95%). Env-configurable, `0` disables.
- **User command `/compact`** — force compaction now. Blocked if processing or compaction already running.
- **MCP tool `compact_context`** — Kesha can trigger it herself when she notices the context is getting heavy (new system_prompt section explains when).
- **User notifications** always show: `🗜 Сжимаю контекст... (было 76%)` → `✅ Контекст сжат: 76% → 12%`.
- **New state `_compacting: set[int]`** — while a chat is compacting, incoming messages go to `_queue` (NOT injected into the in-flight summary request). Drained back into a new batch once compaction finishes.

## v1.5.6 — 2026-04-21

### Fixed
- **Wrong icon for MCP tools** — `mcp__mailru__mail_read` matched the generic `Read` icon (📖) because the matcher used substring containment. Switched to exact `startswith` for built-in tools, and per-server icon lookup for MCP tools (`mcp__<server>__<action>` → icon by server name).
- **Ugly tool names in status bubble** — `mcp__mailru__mail_read` now displays as `mail_read` (shortened). Built-in tool names (`Bash`, `Read`, `Agent`) unchanged.

### Added
- Per-MCP-server icons: 📧 mailru · 🌐 websearch · 🦜 kesha · 📋 yougile · 📄 pandoc · 🏠 aperant · 🐙 github · ⚙️ github-actions. Fallback `🔌` for unknown MCP servers.

### Changed
- **Tool status refresh cadence** 5s → 1s. Users see live timer counting up in real time. TG rate limit is ~1 edit/sec per message, and we already have flood-control handling, so 1s is safe.

## v1.5.5 — 2026-04-20

### Fixed
- **Log timestamps in mixed timezones** — file had some lines in UTC (from systemd service, default TZ) and some in CEST/Europe/Paris (from shell smoke tests when imported with `python -c "import bot"`). Pinned all log timestamps to Krasnoyarsk (UTC+7) via custom `formatTime` regardless of process env.
- **Smoke tests were appending to prod log** — `import bot` from ad-hoc shells attached a FileHandler to the live `logs/kesha.log`. Moved FileHandler attachment behind `__name__ == "__main__" or KESHA_MAIN=1` guard. Smoke tests now only get StreamHandler.

### Changed
- **Daily log rotation** — replaced `RotatingFileHandler(maxBytes=10MB, backupCount=5)` with `TimedRotatingFileHandler(when="midnight", backupCount=7)`. Keeps 7 days of history, matches media cleanup cadence.
- **Auto-cleanup of old log files** — new `cleanup_logs()` removes `kesha.log.YYYY-MM-DD` backups older than 7 days; runs on startup and on a 24h interval alongside `cleanup_media()` via `daily_cleanup_loop`.

## v1.5.4 — 2026-04-20

### Fixed
- **Hanging draft at end of response** — in v1.5.3 `_finalize_text_block` only updated the draft with final Markdown text and relied on a subsequent sendMessage to auto-promote it. When the response ended on pure text (no tool status bubble to follow) the draft trigger `⠀` was sent+deleted too fast for TG to promote → user saw NO text at all, only the "🤖 Сделано" status bubble.
- Changed: keep SendMessageDraft for live streaming animation, but finalize by sending a **real `sendMessage`** with the full final text. This gives us a proper `message_id` to track, and the hanging draft is superseded by the real message on the client.

### Known tradeoff
A visible bubble may briefly flash during the transition as the draft is replaced by the real message. If TG client auto-promotes the draft rather than replacing it, we may see a brief dup — will monitor and iterate.

## v1.5.3 — 2026-04-20

### Changed
- **SendMessageDraft is back** — native Telegram streaming animation restored after the real dup root cause (v1.5.2) was fixed. With `has_deltas` guard in place, the draft → auto-promote pattern no longer double-delivers.
- Flow: `SendMessageDraft(draft_id, text, parse_mode=None)` during streaming → when text block ends, final `SendMessageDraft(..., parse_mode="Markdown")` with last full text → next `sendMessage` in chat (status bubble / next turn's draft / trailing trigger) auto-promotes the draft into a real permanent message.
- **End-of-response trigger**: if a turn ends and no subsequent sendMessage would naturally promote the draft (e.g. stream ended on text with no tool-status bubble after), a zero-width invisible message (`⠀` Braille blank) is sent and immediately deleted to force TG to finalize the hanging draft into a real message.

### Reasoning
editMessageText on a real message (v1.5.1/v1.5.2) worked but flickered. `SendMessageDraft` gives native, smooth character-by-character animation on the Telegram client — same UX as ChatGPT/Claude.ai streaming. With the dup bug fixed at the source (SDK `text` chunks vs `text_delta` chunks), draft's auto-promote behavior is now safe to rely on.

## v1.5.2 — 2026-04-20

### Fixed
- **Real root-cause of duplicate text** — SDK sends BOTH `text_delta` chunks (streaming) AND a final `text` chunk in `AssistantMessage` with the complete text. In v1.5.0 rewrite I dropped the `and not has_deltas` guard on the `text` branch, so both streams appended into `parts` → user saw the same text twice in one bubble. Restored `has_deltas` flag: `text_delta` sets it, `text` is only appended when flag is false. Reset flag on every `_finalize_text_block` for multi-turn responses.
- The previous v1.5.1 draft-removal was a red herring — dup was inside `parts` itself, not in TG delivery.

### Changed
- Tool checkmark is now green emoji `✅` instead of plain `✓`.

## v1.5.1 — 2026-04-20

### Fixed
- **Duplicate text bubbles** — after v1.5.0 if Claude streamed text and then called a tool, the user saw the same text TWICE: once as the auto-finalized draft (TG clients auto-promote a hanging `SendMessageDraft` into a real message when any other `sendMessage` arrives in the chat — including our status bubble), then again via our explicit `_send_safe`. Swapped `SendMessageDraft` + `_send_safe` for straightforward `message.answer` + `edit_message_text` on a real message. No more draft-finalize race, no dup.
- **Final stream bubble keeps its message_id** — when text block finalizes (tool/turn_done/end-of-stream), we now edit the existing streaming message with final Markdown-parsed text instead of sending a brand new message + leaving the streaming one as plain-text orphan.

## v1.5.0 — 2026-04-20

### Changed
- **Live tool status bubble** — all tool calls within one turn now live in a single persistent message with timers, instead of getting overwritten/lost. Shows `⏳ 🖥 Bash \`cmd\` · 12s` while running, `✓` when done. Stall marker `⏱` after 60s. Rate-limited edits (min 5s between) to stay under TG flood control.
- **Removed streaming/tool bubble conflict** — text streaming via `SendMessageDraft` and tool bubbles now live in separate messages. No more `edit_message_text` switching between tool-text and final-text in the same bubble.
- **Deleted dead code** — `_finalize_current_text` with its three-branch edit-in-place/delete-and-resend logic is gone. Now just: `_finalize_text_block` (send as new message) and `_finalize_status` (close out the live tool bubble). Removed `current_msg`, `current_is_tool`, `has_deltas`, `can_edit_in_place`.
- **Per-tool icons** — 🖥 Bash, 📖 Read, ✏️ Write/Edit, 🔎 Glob/Grep, 🌐 WebSearch/WebFetch, 🤖 Agent/Task, 📝 TodoWrite. Fallback `🔧` for unknown.

### Added
- `tool_status.py` with `ToolStatusTracker` — one message, live log of tool calls with running timers, handles rate-limits, flood control, and finalization on turn end.

### Reasoning
Previous UX: user sends a message → tool runs 2-5 min silently → `🔧 Bash ...` bubble gets overwritten each call → no history, no timer → feels hung. Now: full live log visible throughout, timer shows it's alive, all tools retained for context.

## v1.4.2 — 2026-04-18

### Fixed
- **False-positive stall on long tools** — previous `v1.4.1` used a single 120s chunk timeout, which killed legit long-running tool calls (Agent subtasks, deep websearch chains, big Bash operations that take 2–10 min and produce zero chunks from the SDK until they finish). Now two-tier:
  - `TEXT_STALL_TIMEOUT = 90s` — when the last chunk was `text_delta`/`text` (LLM actively writing, silence = real problem)
  - `TOOL_STALL_TIMEOUT = 600s` — when the last chunk was `tool` (tool in progress, SDK is silent by design)
  - Triggered case: Кеша launched an `Agent` subtask at 23:53 to generate FNS XML, subtask took >120s, loop aborted with "⚠️ ответ прервался" even though everything was fine.

## v1.4.1 — 2026-04-18

### Fixed
- **Stream stall / silent response loss** — if Claude SDK stopped producing chunks mid-stream (SSL drop on proxy, etc.), `_ask` hung forever and the user got no reply at all (the draft stayed frozen). Now each chunk is awaited with a 120s timeout; on stall:
  - Partial text is finalized with a `_(⚠️ ответ прервался — повтори если нужно)_` marker
  - Session is reconnected so the next message starts fresh
  - If nothing was ever finalized, user sees `⚠️ Ответ не пришёл (соединение прервалось). Повтори пожалуйста.` instead of silence
  - Triggered case: Катя asked about РКИ on 2026-04-18 23:35, bot streamed into draft, HTTPS proxy dropped, no `ResultMessage` arrived → loop hung, user asked "а где ответ ты че удалил"
- **Draft update dedup** — `_draft_update` now compares full text (not just length) against last sent, and silently swallows `message is not modified` errors instead of spamming DEBUG logs.

## v1.4.0 — 2026-04-14

### Added
- **Multi-user support** — each user gets their own isolated `ClaudeSession` with separate session files (`storage/sessions/<chat_id>`). No more cross-chat message leaking or response mixing. Sessions created lazily on first message.
- **Unknown user response** — unauthorized users get their Telegram ID on first message (once per session), so the owner can easily add them to `ALLOWED_USERS`.
- **Supplements dashboard fixes** — blocked "+" button when inventory is zero, low-stock warning (≤2 days), sorted log by date to fix phantom stock calculation.

### Fixed
- **Cross-chat response mixing** — responses no longer leak between users. Each chat has its own Claude CLI process and streaming pipeline.
- **Phantom inventory in supplements dashboard** — unsorted log caused `max(0, 0-dose) = 0` to silently eat entries. Now sorted before calculation both in data and server code.

### Changed
- `claude_session.py` — `session_file` parameter per instance instead of global `SESSION_FILE`. Migration from old `storage/session_id` supported.
- `reminders.py` — supports callable `get_session(chat_id)` for per-chat inject/processing check.
- Removed `_global_lock` and `_queued_batches` — no longer needed with per-user sessions.

## v1.3.0 — 2026-04-14

### Added
- **msg_id on every message** — single messages now include `[msg_id=X]` tag in prompts, enabling accurate emoji reactions on any message (not just batches).
- **LLM greeting on MCP restart** — when bot is restarted via `restart_bot` MCP tool, Claude writes an in-character greeting instead of static text. Uses file-flag (`storage/greet_on_restart`) with fsync to survive process kill. Normal restarts (systemd/crash) still show plain "Кеша запущен!".
- **Retry with backoff for urgent_llm** — handler retries 3x (15/30/45s delays) on network errors. Fallback to raw text also retries 3x.

### Fixed
- **restart_bot MCP tool** — no longer fails with empty error. Tool now returns immediately ("Bot restarting in 1s...") and schedules the actual `systemctl restart` 1s later via `call_later`, avoiding the race condition where the process kills itself before `communicate()` returns.
- **Emoji reactions on wrong messages** — reactions no longer land on bot's own messages when msg_id is unknown.

### Changed
- `README.md` — added reminders & reactions features documentation (EN + RU), bumped to v1.3.0.

## v1.2.0 — 2026-04-13

### Added
- **Reminders system** (`reminders.py`) — SQLite-backed persistent reminders with 3 types:
  - `plain` — bot sends raw text at the time, no LLM
  - `urgent_llm` — at the time, Claude is triggered (via inject if busy, new turn if idle) to formulate and send the reminder
  - `lazy_llm` — silent at fire time; injected into the next user prompt as context
- **Universal repeat**: `repeat_interval` (`30m`/`2h`/`1d`/`1w`/`3mo`) + optional `repeat_at_time` (`HH:MM`) for daily/weekly alignment.
- **Lazy TTL**: `lazy_llm` reminders not delivered within 24h auto-promote to `urgent_llm`.
- **Missed delivery on startup**: groups missed reminders by type and dispatches accordingly (plain → digest, urgent_llm → Claude turn, lazy_llm → mark fired for next user message).
- **MCP tools**: `create_reminder`, `list_reminders`, `cancel_reminder`, `update_reminder`.
- **Time prefix in prompts**: every prompt now starts with `[YYYY-MM-DD HH:MM +0700]` so Claude has accurate current time in user's timezone (Krsk UTC+7).

### Removed
- `schedule_message` MCP tool — replaced by `create_reminder` (persistent across restarts, supports repeat/cancel/update).

### Changed
- `system_prompt.txt` — added TIME & TIMEZONE and REMINDERS sections explaining the 3 types and how to interpret fired reminder blocks.
- `requirements.txt` — added `python-dateutil` for `relativedelta` (correct month arithmetic).

## v1.1.0 — 2026-04-09

### Fixed
- **Stale response buffer** — injection messages no longer leave orphaned responses in the SDK buffer. Switched from `receive_response()` (stops at first ResultMessage) to `receive_messages()` with manual ResultMessage counting. Each `query()` and `inject()` increments expected results counter; the loop breaks only when all results are consumed.
- **Injection responses merged into single bubble** — each Claude turn (main response + injection responses) now finalizes as a separate Telegram message via `turn_done` signal.

### Changed
- `claude_session.py` — `receive_messages()` + `_expected_results` counter instead of `receive_response()`. Added `_is_processing` flag to prevent injection after response completes.
- `bot.py` — handle `turn_done` chunk type to finalize text between turns.

### How injection works now
1. User sends message → `query()` → `_expected_results = 1`
2. User sends follow-up while Claude is thinking → `inject()` → `_expected_results += 1`
3. `receive_messages()` streams all responses; each `ResultMessage` decrements counter
4. When counter hits 0 → break (all responses consumed, no stale buffer)
5. Each intermediate `ResultMessage` triggers `turn_done` → text finalized as separate TG message

## v1.0.0 — 2026-04-08

### Initial release
- Telegram bot on Claude Agent SDK (ClaudeSDKClient, persistent connection)
- All media types: photo, voice, video, document, audio, sticker, video notes, albums
- Native streaming via SendMessageDraft (Bot API 9.5)
- Message injection while Claude is thinking
- Native interrupt via `/stop`
- Debounce + batching of rapid messages
- Smart tool/text display (tools in ephemeral bubbles)
- Persistent session surviving restarts
- Media cache (file_unique_id, persistent JSON)
- Deepgram Nova-2 STT for voice/video notes
- i18n (RU/EN)
- MCP tools: send_photo, send_file, send_video, send_audio, send_voice, schedule_message, self-config
- Live model switching, context usage tracking
- Auto-retry on session errors
- Global MCP server loading (~/.claude.json, settings.json, .mcp.json)
- Setup wizard for first-run configuration
