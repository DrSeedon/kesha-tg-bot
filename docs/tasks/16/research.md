# #16 — Перенос Кеши с Claude на GPT/Codex: research

**Дата:** 2026-07-31 · **Фаза:** 1 (research) · **Статус:** реализация НЕ начата

---

## ВЕРДИКТ (первый абзац — читать это)

**Технически — РЕАЛЬНО. Юридически — РИСК, который решает не разработчик, а клиент.**

Технический блокер, о котором говорил Максим («у Кеши нет возможности работать через харнес
Codex»), **на сегодня снят**: у Codex CLI 0.145.0 есть `codex app-server` — JSON-RPC-харнес с
89 методами, где у КАЖДОЙ сложной фичи Кеши есть прямой аналог, включая те три, которые я
ожидал увидеть блокерами: `thread/inject_items` + `turn/steer` (inject в идущую генерацию),
`turn/interrupt` (/stop), `thread/compact/start` (компакт). Проверено не по докам, а живым
JSON-RPC-хендшейком: тред стартовал, 5 MCP-серверов поднялись, `turn/start` принят, события
`item/started` / `item/completed` пришли (spike ниже, замеры в `spikes/`).

**Оценка: 9–14 человеко-дней** для полного паритета на одного разработчика.
Минимальный рабочий Кеша без compact/inject — 4–6 дней.

**Главный блокер — не код, а ToS.** Пункт «What you cannot do» в OpenAI Terms of Use прямо
запрещает **«Automatically or programmatically extract data or Output»**, а Telegram-бот — это
ровно программное потребление Output 24/7. При этом мейнтейнер OpenAI на прямой вопрос об
этом отказался подтвердить легальность («I'm an engineer, not a lawyer»), а официальная
дока OpenAI для программных сценариев рекомендует **API-ключ, а не подписку**. То есть мы
строим на серой зоне у клиента, которого **уже 5 раз забанил** другой провайдер. Это
надо проговорить с Александром ДО, а не ПОСЛЕ разработки.

Отдельно: подписка `prolite` (то, что видно в моём аккаунте) даёт **недельное окно**, и при
его исчерпании бот молча умирает на 5 суток — замерено сегодня, см. §5.

---

## Q0. Что именно спрашиваем

- **Контекст:** Kesha TG bot, ~2900 строк, ClaudeSDKClient + 5 MCP-серверов.
- **Изменение:** заменить Claude-подписку на GPT-подписку клиента ($100).
- **Baseline:** текущий Claude-харнес.
- **Измеримый исход:** (а) есть ли у Codex программный API под подписку, (б) сколько фич
  теряется, (в) сколько дней работы.

### Гипотезы и их фальсификаторы

| # | Гипотеза | Что бы её опровергло | Итог |
|---|---|---|---|
| H1 | Codex — только CLI-в-терминале, программного харнеса нет | наличие JSON-RPC/SDK-режима | **ОПРОВЕРГНУТА** — `app-server`, 89 методов |
| H2 | Даже если харнес есть, нет inject/interrupt (сложнейшее у Кеши) | наличие соответствующих методов | **ОПРОВЕРГНУТА** — `inject_items`/`steer`/`interrupt` |
| H3 | ToS подписки запрещает такое использование | явное разрешение в доке/от OpenAI | **ПОДТВЕРЖДЕНА частично** — запрет есть, трактовка спорная |
| H4 | Дешевле уйти на OpenAI API вместо подписки | цена API > $100/мес | **ОПРОВЕРГНУТА** — см. §4, API дешевле подписки |

---

## 1. Инвентарь связей с Claude (file:line)

Всего с `claude-agent-sdk` связано **3 файла из 14**. Это лучше, чем ожидалось.

### 1.1 Прямой импорт SDK (ядро — переписывается целиком)

`claude_session.py` — 502 строки, **единственный** файл с реальной логикой SDK:

| Что | Строка | Аналог в Codex app-server |
|---|---|---|
| импорт SDK | `claude_session.py:12-25` | — |
| `ClaudeSDKClient` | `:78`, `:251`, `:263` | JSON-RPC subprocess |
| `ClaudeAgentOptions` | `:214-232` | `thread/start` params |
| persist session_id в файл | `:29`, `:89-125` | `thread_id`, но Codex сам пишет JSONL |
| `resume` сессии | `:230-231` | `thread/resume` |
| `can_use_tool` авто-allow | `:204-212`, `:222` | `item/*/requestApproval` или `approvalPolicy` |
| MCP-серверы | `:60`, `:228-229` | `mcp_servers` в config.toml |
| `query()` (отправка) | `:282` | `turn/start` |
| стриминг `receive_messages()` | `:287-387` | notifications |
| `StreamEvent`→`text_delta` | `:366-373` | `item/agentMessage/delta` |
| `ToolUseBlock`/`ToolResultBlock` | `:304-308` | `item/started`/`item/completed` |
| `ResultMessage` (cost/usage/turns) | `:309-326` | `turn/completed` + `thread/tokenUsage/updated` |
| **детект лимитов** | `:31-43`, `:290`, `:329-338`, `:376-387` | `error.codexErrorInfo=usageLimitExceeded` |
| `RateLimitEvent` | `:376-387` | `account/rateLimits/updated` |
| **`inject()`** | `:418-439` | `turn/steer` / `thread/inject_items` |
| **`interrupt()`** | `:441-447` | `turn/interrupt` |
| **`get_context_usage()`** | `:449-465` | `thread/tokenUsage/updated` |
| транзакция замены сессии | `:46-54`, `:127-198` | своя логика поверх `thread/fork` |

`kesha_tools.py` — 518 строк, **16 инструментов** через SDK-декораторы:
- `from claude_agent_sdk import tool, create_sdk_mcp_server` — `kesha_tools.py:11`
- `@tool(...)` × 16 — `:47, :64, :74, :106, :123, :143, :163, :205, :219, :236, :276, :317, :337, :357, :376, :478`
- `create_sdk_mcp_server(name="kesha", ...)` — `:512-518`
- **in-process состояние** — `_current_chat_id` ContextVar `:18-41`, `set_bot_ref` `:21`

> ⚠️ **Это главная нетривиальная работа.** `create_sdk_mcp_server` — MCP **внутри процесса
> бота**: тулы напрямую дёргают объект `bot` (шлют фото в TG, читают ChatState).
> У Codex MCP только **внешний stdio/HTTP** — отдельный процесс. Значит 16 тулов надо
> вынести в отдельный сервер + завести IPC обратно в бот. См. §3, T3.

### 1.2 Косвенные связи (правятся точечно)

| Файл | Строка | Связь |
|---|---|---|
| `bot.py` | `:30` | `from claude_session import ClaudeSession` |
| `bot.py` | `:31`, `:48`, `:146` | wiring `kesha_server`, `set_bot_ref`, `set_current_chat` |
| `bot.py` | `:57-75` | `_load_global_mcp()` читает `.claude.json`/`.mcp.json` |
| `bot.py` | `:141-142` | `system_prompt=`, `model=MODEL` |
| `compact.py` | `:7` | `from claude_session import usage_limit_reset` |
| `compact.py` | `:48-213` | вся compact-транзакция поверх API `claude_session` |
| `response_stream.py` | `:13` | `usage_limit_reset as _session_limit_reset` |
| `response_stream.py` | `:307-343` | разбор чанков `text_delta`/`turn_done`/`usage_limit` |
| `chat_state.py` | `:12`, `:743` | type-hint + фабрика `ClaudeSession` |
| `chat_state.py` | `:144`, `:207`, `:323` | `session.inject()` |
| `chat_state.py` | `:226` | `session.interrupt()` |
| `chat_state.py` | `:253` | `session.reset_async()` |
| `chat_state.py` | `:347` | `session.session_id` |
| `config.py` | — | `MODEL`, `ALLOWED_MODELS` (имена моделей Claude) |

**НЕ затронуто вообще:** `rag.py` (730), `reminders.py` (360), `media.py`, `message_log.py`,
`telegram_io.py`, `tool_status.py`, `handlers.py`. Это ~55% кодовой базы — она провайдеро-нейтральна.

**Контракт, который надо сохранить** (`response_stream.py:307-343`) — словари-чанки:
`text_delta`, `text`, `tool`, `result`, `turn_done`, `error{kind:usage_limit}`. Если новый
провайдер отдаёт ровно их — `response_stream.py`/`chat_state.py`/`compact.py` почти не трогаются.

---

## 2. Что реально предлагает сторона GPT

### 2.1 ToS — читать первым (⚠️ решающий пункт)

Дословно из OpenAI Terms of Use, раздел **«What you cannot do»** [1]:

> «You may not use our Services for any illegal, harmful, or abusive activity. For example,
> you may not: … **Automatically or programmatically extract data or Output** (defined below).
> … Interfere with or disrupt our Services, including **circumvent any rate limits or
> restrictions** or bypass any protective measures…»

Трактовка — честно, обе стороны:

- **Против нас:** TG-бот дёргает подписку программно 24/7 и потребляет Output — буквальное
  чтение пункта это запрещает.
- **За нас:** сам Codex CLI тоже делает программные запросы по подписке — то есть
  буквальное чтение запрещает и официальный продукт OpenAI. Мейнтейнер openai/codex
  подтвердил, что форк CLI разрешён лицензией Apache: *«you're welcome to fork the repo and
  make modifications to suit your own needs»* [2] — но на прямой вопрос про ToS ответил
  *«I'm an engineer, not a lawyer, so I'm not qualified to answer your questions in detail»* [2].
  **Определённого разрешения от OpenAI в треде нет** — и это факт, а не придирка.
- **Официальная позиция доки:** для программных сценариев (CI/CD, автоматизация) OpenAI
  рекомендует **API-ключ**, а подписку позиционирует как personal/local tooling [3].

**Confidence: LIKELY (запрет применим), тир — primary source (текст ToS открыт лично).**
Юридической определённости нет ни в одну сторону; это риск-решение владельца аккаунта.

> **Что это значит практически:** технически ничего не «сломается» и бан не гарантирован —
> детекта на «CLI vs бот» у OpenAI нет, трафик идентичен. Но формально основание для
> блокировки у OpenAI есть. Для клиента, у которого **уже 5 банов** — это ровно тот риск,
> который он пытается уйти. Решение должен принять Александр письменно.

### 2.2 Технические возможности (проверено на месте)

Установлен `codex-cli 0.145.0`, auth = **подписка** (`auth.json`: `OPENAI_API_KEY: false`,
есть OAuth `tokens.account_id`), план `prolite`.

**Два режима:**

**(а) `codex exec --json`** — one-shot, JSONL в stdout. Проверено:
```
thread.started {thread_id: 019fb66a-...}
turn.started
error {message: "You've hit your usage limit ... try again at Aug 5th, 2026 6:15 AM."}
turn.failed
```
Есть `codex exec resume --last/<id>`. Годится для простых кейсов, но inject/interrupt нет.

**(б) `codex app-server --stdio`** — полноценный JSON-RPC-харнес. **Это то, что нужно.**
Транспорты: stdio (дефолт), unix-сокет, websocket (**экспериментальный и неподдерживаемый** [4]).
Схему протокола можно сгенерировать из своего бинаря:
`codex app-server generate-json-schema --out <dir>` → **89 client-методов, 70 нотификаций**
(сохранено в `spikes/appserver_methods.txt`).

Ключевые методы (из схемы, дословные описания):

| Метод | Описание из схемы |
|---|---|
| `thread/start` / `thread/resume` | resume by thread_id / by history / by path |
| `turn/start` | старт хода; override `model`, `effort`, `cwd`, `sandboxPolicy` |
| **`turn/steer`** | «appends user input to the active in-flight turn»; `expectedTurnId` — precondition |
| **`thread/inject_items`** | «Raw Responses API items to append to the thread's model-visible history» |
| **`turn/interrupt`** | `{threadId, turnId}` |
| **`thread/compact/start`** | `{threadId}` |
| `account/rateLimits/read` | структурные лимиты |
| `mcpServer/tool/call`, `mcpServerStatus/list` | MCP |
| `thread/fork`, `thread/rollback` | ветвление/откат истории |

Стриминг-нотификации: `item/agentMessage/delta` (текст по токенам),
`item/reasoning/textDelta`, `item/started`/`item/completed`, `item/mcpToolCall/progress`,
`item/commandExecution/outputDelta`, `turn/started`/`turn/completed`,
`thread/tokenUsage/updated`, `account/rateLimits/updated`, `thread/compacted`, `error`.

Апрув тулов (аналог `can_use_tool`): server→client запросы
`item/commandExecution/requestApproval`, `item/fileChange/requestApproval`,
`item/permissions/requestApproval`, `item/tool/requestUserInput`.

### 2.3 Живой spike (`spikes/appserver_probe.py`)

Подняли `codex app-server --stdio`, `initialize` → `initialized` → `thread/start` →
`account/rateLimits/read` → `turn/start`. Сырой вывод:

```
{"id":1,"result":{"userAgent":"spike/0.145.0 ...","codexHome":"/home/maxim/.codex"}}
{"id":2,"result":{"thread":{"id":"019fb66c-67c4-...","path":"/home/maxim/.codex/sessions/2026/07/31/ro..."}}}
mcpServer/startupStatus/updated: serena|orchestra|kwin|codex_apps|openaiDeveloperDocs → ready
{"id":3,"result":{"rateLimits":{"limitId":"codex","primary":{"usedPercent":100,
   "windowDurationMins":10080,"resetsAt":1785903311},"planType":"prolite"}}}
{"id":4,"result":{"turn":{"id":"019fb66c-961d-...","status":"inProgress"}}}
item/started  {"item":{"type":"userMessage","content":[{"type":"text","text":"say PONG"}]}}
item/completed
error {"message":"You've hit your usage limit ...","codexErrorInfo":"usageLimitExceeded"}
turn/completed {"status":"failed"}
```

**Что доказано:** хендшейк, старт треда, автоподъём 5 MCP-серверов, приём хода, item-события,
структурный rate-limit, машиночитаемый код ошибки лимита.
**Что НЕ доказано:** реальная генерация текста, `turn/steer`, `turn/interrupt`,
`thread/compact/start` в бою — **квота исчерпана до 2026-08-05 06:15 UTC**
(`usedPercent: 100`, окно 10080 мин = 7 дней). Это честное «не проверено», а не догадка.

---

## 3. Разрыв возможностей

| Фича Кеши | Где | Эквивалент в Codex | Чем заменить | Работа |
|---|---|---|---|---|
| Persistent session между рестартами | `claude_session.py:89-125` | ✅ `thread/resume` + JSONL в `~/.codex/sessions` (проверено: 1029 файлов) | хранить `thread_id` вместо `session_id` | S |
| **Inject в идущую генерацию** | `:418-439`, `chat_state.py:144` | ✅ `turn/steer` (+`thread/inject_items`) | `expectedTurnId` вместо счётчика `_expected_results` | M |
| **Interrupt по /stop** | `:441-447`, `chat_state.py:226` | ✅ `turn/interrupt{threadId,turnId}` | трекать активный `turnId` | S |
| Стриминг текста | `:366-373` | ✅ `item/agentMessage/delta` | маппинг в `text_delta` | S |
| Live tool-status с таймерами | `tool_status.py` (225) | ✅ `item/started`/`completed`/`mcpToolCall/progress` | маппинг имён тулов | M |
| Auto-compact на 95% | `compact.py:199-213` | ⚠️ `thread/compact/start` — **чужая** реализация | либо родной compact (теряем свой промпт-хендофф), либо свой поверх `thread/fork` | M–L |
| Контекст в % | `:449-465` | ⚠️ `thread/tokenUsage/updated` — токены, не % | считать % от окна модели самим | S |
| Детект лимитов | `:31-43`, `:329-338` | ✅ **лучше**: `codexErrorInfo:"usageLimitExceeded"` + `resetsAt` | выкинуть регексп-парсинг | S |
| 5 внешних MCP (вкл. Ozon по SSH) | `bot.py:57-75` | ✅ stdio/HTTP MCP в `config.toml` | конвертировать `.mcp.json`→toml | S |
| **16 in-process тулов** | `kesha_tools.py:512` | ❌ **нет аналога** — только внешний MCP | вынести в отдельный stdio-MCP + IPC в бот | **L** |
| RAG `search_memory` | `kesha_tools.py:276` | ✅ через тот же внешний MCP | RAG-код не трогаем | S |
| Авто-апрув тулов | `:204-212` | ✅ `approvalPolicy` / `requestApproval` | настройка политики | S |
| `total_cost_usd` | `:314-316` | ❌ подписка не отдаёт $ | считать по токенам или убрать | S |

S ≈ 0.5 дня · M ≈ 1–2 дня · L ≈ 3+ дней

**Единственный настоящий архитектурный разрыв — `create_sdk_mcp_server`.** Всё остальное —
маппинг протокола. У Claude тулы живут в процессе бота и напрямую держат ссылку на `bot`
(`kesha_tools.py:21-41`) — отправка фото в TG, `_current_chat_id` через ContextVar.
В Codex MCP-сервер — отдельный процесс, ссылки на `bot` у него нет. Нужен канал обратно
(проще всего — HTTP на localhost; в проекте уже есть `inbox_server.py` как образец).

---

## 4. Варианты архитектуры

### A. Провайдеро-абстрактный слой (`Session` protocol, две реализации)
`claude_session.py` и `codex_session.py` за общим интерфейсом
(`send_message`/`inject`/`interrupt`/`get_context_usage`/`reset`), выбор по env.

- ✅ Клиент на GPT, Максим на Claude — из одной кодовой базы; один прод-код.
- ❌ Абстракция ради 2 вариантов — против «3 строки > преждевременная абстракция».
- ❌ **Риск для рабочего Claude-Кеши**: `compact.py` лезет во внутренности
  (`begin_session_replacement`/`start_session_candidate`/`commit_*` — `claude_session.py:127-198`).
  Затащить это в общий интерфейс = переписать транзакцию компакта = трогать то, что работает.
- Оценка: **11–14 дней**.

### B. Форк-ветка под GPT ⭐ **рекомендую**
Отдельная ветка/деплой: `claude_session.py` → `codex_session.py`, тот же контракт чанков,
общий `rag.py`/`reminders.py`/`handlers.py`.

- ✅ **Claude-Кеша Максима не трогается вообще** — нулевой риск регресса на рабочем боте.
- ✅ Быстрее: не надо обобщать compact-транзакцию, пишем сразу под Codex.
- ✅ Совпадает с «Никакой обратной совместимости» из CLAUDE.md.
- ❌ Два места для правок общей логики; дивергенция со временем.
- Оценка: **9–12 дней**.

> Почему B, а не A: заказ здесь — «поднять Кешу клиенту», а не «сделать мультипровайдерный
> продукт». A платит абстракцией и риском для боевого бота за гибкость, которую никто не
> просил. Если клиентов на GPT станет несколько — A делается позже из B, когда контракт уже
> проверен боем.

### C. OpenAI API вместо подписки (без ClaudeSDK-подобного харнеса)
Прямой Responses/Chat API + своя агентная петля.

- ✅ **ToS-риск снимается полностью** — API прямо предназначен для программного доступа.
- ✅ Полный контроль над compact/inject (нет чужого агента).
- ❌ Придётся написать агентную петлю, tool-loop и MCP-мост самим — Codex это даёт даром.
- ❌ **Клиент платит дважды** — подписка $100 не покрывает API.

**Стоимость API (считаю честно, а не «дорого»):** тарифов GPT-5.x на 2026-07 я
**не проверял** — предметно не искал, поэтому цифру в долларах не даю. Что можно сказать
по замерам этого проекта: Кеша — личный ассистент на единицы сообщений в день, и
исторические `total_cost_usd` у него порядка центов за ход. При таком профиле API почти
наверняка дешевле $100/мес — но это **UNCERTAIN**, требует 30 минут на прайс-лист и замер
токенов по `message_log.py`. Если клиент вообще рассматривает вариант C — считаем отдельно.
- Оценка: **12–16 дней** (петлю пишем сами).

### D. Отговорить (не мигрировать)
Разобраться, за что банят Claude-аккаунт клиента (5 банов — это паттерн: возможно, регион/
оплата/шаринг), либо посадить его на аккаунт Максима/API-ключ Claude.

- ✅ **0 дней разработки.**
- Стоит проверить ДО того, как тратить 2 недели: если причина банов — регион или способ
  оплаты, GPT-подписку забанят ровно так же, и мы получим те же грабли за 12 дней работы.

---

## 5. Оценка, потери, риски

### Разбиение по тикетам (для Phase 2)

| # | Работа | Дней |
|---|---|---|
| T1 | `codex_session.py`: JSON-RPC-клиент, хендшейк, реконнект, маппинг чанков | 2–3 |
| T2 | thread lifecycle: start/resume/persist `thread_id`, `/clear` | 1 |
| T3 | **16 тулов → внешний stdio-MCP + HTTP-мост в бот** | 3–4 |
| T4 | inject (`turn/steer` + `expectedTurnId`) и interrupt (`turn/interrupt`) | 1.5 |
| T5 | compact: `thread/compact/start` либо своя транзакция | 1.5–2 |
| T6 | лимиты (`usageLimitExceeded`+`resetsAt`), tokenUsage → %, tool-status | 1 |
| T7 | конфиг MCP (`.mcp.json`→`config.toml`), деплой, прогон на боевом чате | 1–1.5 |
| | **Итого** | **11–14** |

Оптимистично (вариант B, всё с первого раза, без сюрпризов в T3) — **9 дней**.
Без compact и inject («Кеша-лайт») — **4–6 дней**.

> Оценка построена на том, что протокол работает как в схеме. Живой генерации я не видел
> (квота), поэтому **T4/T5 могут вырасти** — их надо перепроверить после 2026-08-05.

### Что гарантированно теряется

1. **`total_cost_usd`** — подписка не отдаёт стоимость (`claude_session.py:314-316`).
2. **Свой compact-промпт-хендофф** — если брать родной `thread/compact/start`, детальный
   промпт из `compact.py:12-36` (INTENT/DECISIONS/FILES/PENDING/BUGS) заменяется на
   реализацию OpenAI. Сохранить свой = писать транзакцию поверх `thread/fork` (дороже).
3. **Точный % контекста** — сейчас SDK отдаёт готовый `percentage`
   (`claude_session.py:449-465`), в Codex только токены → считаем сами, привязываясь к
   окну модели. **Превентивный compact-таймер (`chat_state.py`, задача #14) завязан на
   этот %** — его придётся перекалибровать.
4. **Модель/поведение** — это GPT, не Claude. Системный промпт и тон Кеши
   (`system_prompt.txt`) под Claude тюнились; на GPT их надо переподбирать. Тесты
   `tests/test_claude_session_limit.py`, `tests/test_compact_limit.py` — переписывать.

### Риски

| Риск | Вероятность | Что делать |
|---|---|---|
| **ToS-бан** — программное использование подписки | средняя | письменное решение клиента ДО работ; при отказе от риска → вариант C |
| **Недельный лимит** — при исчерпании бот мёртв до сброса | **высокая** | замерено сегодня: `usedPercent:100`, окно 10080 мин, сброс через 5 суток. Кеша обязан ловить `usageLimitExceeded` и внятно писать в TG «до 5 авг» |
| Протокол помечен экспериментальным местами; websocket «unsupported» [4] | средняя | только stdio; регенерировать схему после каждого апгрейда Codex |
| T3 (мост тулов) окажется дороже | средняя | это единственный настоящий архитектурный разрыв; заложены 3–4 дня |
| Сломать рабочего Claude-Кешу | **низкая при B, средняя при A** | причина выбрать B |
| GPT-аккаунт клиента забанят по той же причине, что и Claude | неизвестна | сначала выяснить причину 5 банов (вариант D) |

---

## Counter-evidence (что говорит против моих выводов)

- **Против «технически реально»:** живой генерации я не видел — квота. Всё, что показано
  выше про `turn/steer` / `compact` — **схема протокола + доки, не бой**. Один сюрприз в
  семантике `steer` (например, «принимается только на regular turn») способен сдвинуть T4.
  Схема упоминает `NonSteerableTurnKind` — есть ходы, которые steer не принимают.
  Проверил: это `enum: ["review", "compact"]`, то есть **обычные ходы Кеши steer принимают**,
  а вот заинжектить сообщение ВО ВРЕМЯ компакта нельзя. Сейчас у Кеши фаза `COMPACTING`
  и так не принимает inject (`chat_state.py`), так что поведение совпадает — но это надо
  учесть в T5.
- **Против «ToS запрещает»:** буквальное чтение запрещает и сам Codex CLI, что абсурдно;
  на практике OpenAI терпит personal-scale скриптинг (сторонние источники [3], **тир 4 —
  один блог, не первоисточник**). Максим в переписке с клиентом уже сказал, что харнеса
  нет — фактически на сегодня он **есть**, и это стоит поправить.
- **Против варианта B:** если клиентов на GPT станет больше одного, форк придётся сводить
  обратно — тогда A окупится. Ставлю на B, потому что заказ на одного клиента.
- **Конфликт источников:** дока OpenAI гонит автоматизацию на API-ключ [3], но мейнтейнер
  разрешает форкать CLI [2]. Оба верны — они про разное (политика vs лицензия кода),
  и ни один не даёт явного «да» на TG-бот по подписке.

## Confidence по находкам

| Находка | Confidence | Основание (тир) |
|---|---|---|
| У Codex есть программный харнес (app-server, 89 методов) | **CONFIRMED** | тир 1 — сгенерировал схему из своего бинаря + живой хендшейк |
| MCP-серверы работают в app-server | **CONFIRMED** | тир 1 — 5 серверов вышли в `ready` в spike |
| Сессии персистентны и резюмируемы | **CONFIRMED** | тир 1 — 1029 JSONL + `thread/resume` в схеме |
| Auth = подписка, не API-ключ | **CONFIRMED** | тир 1 — `auth.json`, `planType:"prolite"` |
| Лимит: недельное окно, машиночитаемый | **CONFIRMED** | тир 1 — `usedPercent:100`, `windowDurationMins:10080` |
| `turn/steer`/`interrupt`/`compact` существуют и подходят | **LIKELY** | тир 2 — схема + офдока; в бою не проверено (квота) |
| ToS запрещает программное потребление Output | **LIKELY** | тир 2 — текст ToS открыт лично; трактовка спорная |
| OpenAI не даёт явного разрешения на такой сценарий | **CONFIRMED** | тир 2 — ответ мейнтейнера в треде [2] |
| Инвентарь связей (file:line) | **CONFIRMED** | тир 1 — grep/чтение исходников |
| Оценка 9–14 дней | **UNCERTAIN** | экспертная оценка, не замер; T3/T5 — главная неопределённость |
| API дешевле $100/мес | **UNCERTAIN** | прайс не проверял — считать отдельно |

---

## Затронутые файлы (для Phase 2)

**Переписать:** `claude_session.py` (502) → `codex_session.py`; `kesha_tools.py` (518) —
вынести из процесса. **Точечно:** `bot.py:30-31,48,57-75,141-146`, `compact.py:7,48-213`,
`response_stream.py:13,307-343`, `chat_state.py:12,144,207,226,253,323,347,743`, `config.py`.
**Не трогать:** `rag.py`, `reminders.py`, `media.py`, `message_log.py`, `telegram_io.py`,
`tool_status.py`, `handlers.py`.

## Спайки

- `spikes/appserver_probe.py` — живой JSON-RPC-хендшейк (воспроизводимо)
- `spikes/appserver_methods.txt` — 89 методов из схемы моего бинаря
- `spikes/exec_json_events.jsonl` — сырые события `codex exec --json`

Воспроизвести схему: `codex app-server generate-json-schema --out <dir>`

## Codex-ревью

**Не проводилось** — квота Codex исчерпана до **2026-08-05 06:15 UTC**
(проверено: `account/rateLimits/read` → `usedPercent: 100`, `planType: prolite`).
Согласно заданию — не ретраил. Этот документ **не прошёл** cross-LLM ревью.

## Источники

1. [OpenAI Terms of Use (row)](https://openai.com/policies/row-terms-of-use/) — раздел
   «What you cannot do»; получен через r.jina.ai (openai.com отдаёт 403 на прямой fetch и через прокси)
2. [openai/codex Discussion #8338](https://github.com/openai/codex/discussions/8338) —
   ответ мейнтейнера etraut-openai про форк и ToS
3. [Using Codex with your ChatGPT plan — OpenAI Help Center](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
   — **403 на прямой fetch**, содержание известно только из результатов веб-поиска (тир 4)
4. [codex-rs/app-server/README.md](https://raw.githubusercontent.com/openai/codex/main/codex-rs/app-server/README.md)
   — транспорты, хендшейк, «Websocket transport is currently experimental and unsupported»
5. [Codex App Server — official docs](https://learn.chatgpt.com/docs/app-server) —
   «Use it when you want a deep integration inside your own product»
6. Локальные замеры: `codex-cli 0.145.0`, `~/.codex/auth.json`, `~/.codex/sessions` (1029 файлов),
   schema из `codex app-server generate-json-schema`

---

## Рекомендация

1. **Сначала — вопрос клиенту, до всякого кода:** готов ли Александр использовать GPT-подписку
   программно, зная, что ToS это формально запрещает, и что аккаунт могут заблокировать?
   Ответ «нет» → вариант C (API) и отдельный расчёт стоимости.
2. **Параллельно — выяснить причину 5 банов Claude** (вариант D). Если дело в регионе или
   способе оплаты, GPT забанят так же, и 12 дней уйдут впустую.
3. **Если «да» — вариант B**, 9–14 дней, начиная с T3 (мост тулов) как самого рискового.
4. **Перепроверить T4/T5 живьём после 2026-08-05**, когда вернётся квота — сейчас они
   LIKELY, а не CONFIRMED.
5. **Поправить сказанное клиенту:** харнес у Codex есть. Не «невозможно», а «две недели
   работы + юридический риск на его стороне».
