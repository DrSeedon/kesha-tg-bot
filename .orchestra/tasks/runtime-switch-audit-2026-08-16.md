# Аудит переключения Claude ↔ Codex — 2026-08-16

## Вердикт

Транспортный таймаут Codex исправлен и проверен на боевой 65-ходовой сессии.
Сам runtime switch безопасно откатывается при большинстве ошибок, но до полноценного
двухпользовательского режима не хватает четырёх вещей:

1. durable-выбора рантайма для каждого чата;
2. строгого readiness-контракта;
3. симметричного переноса истории без дублей;
4. ясной семантики `/clear` для обеих native-сессий.

## Что работает сейчас

- `ChatRegistry` создаёт отдельный `ChatState` для каждого `chat_id`.
- В личных чатах Максим и Катя уже могут одновременно сидеть на разных рантаймах.
- В общей группе рантайм общий для всей группы, потому что ключом служит `chat.id`.
- Claude и Codex имеют отдельные native session ID:
  - Claude: `storage/sessions/<chat_id>`;
  - Codex: `storage/sessions/<chat_id>.codex`.
- Candidate сначала запускается и проверяется; старый backend отключается только после
  успешного принятия нового.
- При ошибке новый backend уничтожается, старый остаётся рабочим.
- Switch запрещён во время turn/stop/compact; сообщения и напоминания не теряются.

Важно: выбор рантайма независимый, но квоты общие. Оба чата расходуют одну Claude-подписку
и одну Codex/ChatGPT-подписку.

## Findings

### P1 — активный рантайм не переживает рестарт

`ChatState.runtime_id` меняется только в памяти (`chat_state.py:388-389`). При новом
процессе `ChatRegistry.get()` снова строит каждый чат на глобальном `KESHA_RUNTIME`
(`chat_state.py:1367-1382`). Native session ID сохраняются, активный выбор — нет.

Следствие: после рестарта Максим и Катя снова оказываются на Claude, даже если до него
один из них выбрал Codex.

### P1 — readiness-probe может принять неготовый target

`ChatState._probe_runtime()` считает часть отрицательных результатов
`check_context_reserve()` успехом (`chat_state.py:523-543`). Ветка Codex вызывает
`read_quota()`, но не проверяет `usage_limit_active` после ответа.

Подтверждённые ложноположительные случаи:

- `runtime_unhealthy` → switch разрешён;
- `runtime_invariant` → switch разрешён;
- `rateLimitReachedType`/`usage_limit_active=True` → switch разрешён.

Нужен единый `probe_readiness()` в runtime-протоколе с явным результатом: transport,
auth, quota, context и reason.

### P1 — `/clear` очищает только активный backend

`request_clear()` вызывает `reset_async()` только у `self.session`
(`chat_state.py:296-323`). Session-файл второго backend и общий `message_log` остаются.
После `/clear` переключение может вернуть старую native-историю или снова передать её
через handoff.

Нужна явная семантика:

- `/clear` — очистить обе native-сессии, pending handoff и поставить history floor;
- при реальной необходимости отдельная `/clear_current` — только текущий backend.

### P2 — перенос истории асимметричен

Claude → Codex переносит до 40 последних сообщений через безопасный
`thread/inject_items`. Codex → Claude возвращается в старую Claude-сессию без событий,
произошедших во время работы Codex, потому что Claude adapter объявляет
`passive_handoff=False`.

### P2 — повторный handoff дублирует контекст

Каждый Claude → Codex снова берёт последние 40 сообщений. Cursor/watermark целевого
runtime отсутствует, поэтому повторные переключения инжектят уже виденную историю и
ускоряют заполнение контекста.

### P2 — текущий UX требует помнить синтаксис

Есть только `/runtime` и `/runtime claude|codex`. Нет прямых `/claude`, `/codex`,
inline-кнопок и отдельного быстрого обновления квоты. `/runtime` может ждать live quota,
хотя статус-панель лучше отдавать сразу из кеша.

## Что показывает внешний research

Native session ID разных поставщиков несовместимы. OpenAI Codex хранит собственный thread
и поддерживает `thread/resume`, `thread/fork`, `thread/compact/start` и
`thread/inject_items`. Claude Agent SDK хранит локальный JSONL transcript и продолжает его
через `resume=session_id`; импорт Codex thread ID в Claude не документирован.

Практический паттерн у multi-provider приложений другой:

1. приложение владеет canonical transcript;
2. provider adapter владеет native session ID;
3. при смене target получает только ещё не виденный delta;
4. большие разрывы передаются как структурированный summary плюс короткий verbatim tail;
5. reasoning и сырые provider-specific tool payload не переносятся.

LangGraph отдельно разводит thread-scoped state и user-scoped memory. Для Кеши не нужен
сам LangGraph, но разделение правильное: диалог и runtime — по чату, долговременная память —
по пользователю/домену.

## Рекомендуемая архитектура

### Durable runtime preference

```sql
CREATE TABLE chat_runtime_state (
    chat_id INTEGER PRIMARY KEY,
    active_runtime TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- `KESHA_RUNTIME` используется только как дефолт нового чата.
- `ChatRegistry.get(chat_id)` загружает сохранённый runtime и валидирует его через registry.
- После probe и handoff target записывается в SQLite; ошибка записи откатывает switch.
- Durable write является commit point: после него рестарт обязан поднять target.

### Canonical history и cursors

```sql
CREATE TABLE runtime_sync_cursor (
    chat_id INTEGER NOT NULL,
    runtime_id TEXT NOT NULL,
    seen_through_message_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, runtime_id)
);
```

- `messages.db` остаётся source of truth видимого диалога.
- Native Claude/Codex sessions — локальные оптимизации и точная provider-history.
- При switch переносится только `messages.id > seen_through_message_id` target-runtime.
- Cursor двигается только после успешного импорта или успешного следующего turn.
- Для Codex delta попадает через `thread/inject_items`.
- Для Claude durable handoff добавляется к следующему настоящему пользовательскому prompt;
  отдельный agentic turn на переключении запрещён.
- Tool results переносятся кратким итогом, без reasoning и сырых мегабайтных payload.

### Telegram UX

Один controller переключения вызывается четырьмя входами:

- `/runtime` — статус и панель;
- `/claude` — идемпотентно выбрать Claude;
- `/codex` — идемпотентно выбрать Codex;
- `/runtime claude|codex` — обратная совместимость.

Панель:

```text
🧩 Рантайм этого чата: Codex
Модель: gpt-5.6-sol
Лимит: 7d 28%, сброс через 4д 3ч

[ Claude ] [ ✓ Codex ]
[ 🔄 Обновить лимит ]
```

Callback обязан сразу вызвать `answerCallbackQuery`, затем показать
`⏳ Переключаю…` и отредактировать то же сообщение итогом. Toggle-команда не нужна:
явные `/claude` и `/codex` идемпотентны и безопасны при повторной доставке.

При исчерпании квоты лучше показывать кнопку `Продолжить на Codex`, а не делать тихий
auto-failover. После уже выведенного текста или выполненного tool call автоматический replay
может повторить побочный эффект.

## Порядок реализации

### Этап 1 — correctness

1. `probe_readiness()` и отказ при unhealthy/invariant/exhausted.
2. Persisted per-chat runtime.
3. `/clear` очищает обе ветки и ставит history floor.
4. Тесты двух чатов и рестарта.

### Этап 2 — UX

1. `/claude` и `/codex`.
2. Inline-панель `/runtime`.
3. Cached quota + отдельное обновление.
4. Кнопка ручного fallback при лимите.

### Этап 3 — единая история

1. `runtime_id`/`turn_id` в canonical transcript metadata.
2. Per-runtime cursor и durable pending handoff.
3. Delta transfer в обе стороны.
4. Bounded summary + последние 6–12 видимых сообщений для больших разрывов.

## Acceptance criteria

- Максим выбирает Codex, Катя остаётся на Claude.
- После рестарта оба получают свой сохранённый runtime.
- Switch одного chat ID не меняет второй и не отзывает его bridge handles.
- Unhealthy, invalid или exhausted target не становится активным.
- Ошибка SQLite/handoff оставляет старый runtime рабочим и сохранённым.
- `/claude`, `/codex`, текстовая команда и кнопка используют один code path.
- Двойной/stale callback не запускает второй процесс.
- Claude → Codex → Claude сохраняет сообщения обоих участков.
- Один delta не импортируется повторно после десяти переключений.
- `/clear` не позволяет старой истории воскреснуть.
- Сообщение или напоминание во время switch доставляется ровно один раз.

## Источники

- OpenAI Codex App Server: https://developers.openai.com/codex/app-server
- OpenAI conversation state: https://developers.openai.com/api/docs/guides/conversation-state
- Claude Agent SDK session browser: https://platform.claude.com/cookbook/claude-agent-sdk-05-building-a-session-browser
- Anthropic Messages API: https://platform.claude.com/docs/en/build-with-claude/working-with-messages
- Telegram Bot API callbacks: https://core.telegram.org/bots/api#callbackquery
- Telegram bot features: https://core.telegram.org/bots/features
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Vercel AI SDK provider registry: https://ai-sdk.dev/docs/reference/ai-sdk-core/provider-registry
- Vercel AI SDK message persistence: https://ai-sdk.dev/docs/ai-sdk-ui/chatbot-message-persistence
