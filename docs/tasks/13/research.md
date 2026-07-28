# #13 — Compact при исчерпанном Claude Code usage limit

## Question

**Контекст:** Kesha использует persistent `ClaudeSDKClient`; ручной, preventive и
auto-compact сначала просят текущую сессию создать handoff summary, затем сбрасывают
session и загружают summary в новую.

**Изменение под проверкой:** корректно классифицировать usage/rate-limit во всех
формах, которые отдаёт установленный Agent SDK/CLI, и завершать compact без потери
старой сессии и без сырой ошибки в Telegram.

**Baseline:** текущий код трактует каждый `AssistantMessage.TextBlock` как нормальный
текст, а `compact_session()` различает лимит только как поздний строковый
`{"type": "error"}`.

**Измеримый результат:** при лимите `reset_async()` не вызывается, исходный
`session_id` остаётся записан, progress получает терминальное состояние, Telegram
получает один короткий текст без raw SDK error и `📋`, а автоматический compact не
делает второй заведомо обречённый summary-запрос после уже распознанного лимита.

## Hypotheses considered

### H1 — compact зависает внутри coroutine

**Falsifier:** журнал показывает возврат `COMPACTING → IDLE` сразу после ошибки.

**Результат:** REFUTED. На инциденте coroutine завершилась и state machine вернулась
в `IDLE`; «висящим» осталось представление progress в Telegram, не серверная фаза.

### H2 — SDK уже даёт typed signal, но wrapper его теряет

**Falsifier:** production transcript не содержит typed `AssistantMessage.error`, либо
`claude_session.py` уже проверяет это поле до выдачи `TextBlock`.

**Результат:** CONFIRMED. Transcript содержит top-level `error="rate_limit"`, а
wrapper выдаёт synthetic error text как обычный `text`, затем повторно как `error`.

### H3 — строкового поиска только в `compact.py` достаточно

**Falsifier:** SDK имеет несколько независимых typed surfaces или raw limit text
может прийти как обычный `TextBlock` без последующего error result.

**Результат:** REFUTED. У SDK есть `AssistantMessage.error`,
`ResultMessage.api_error_status`, `ResultMessage.terminal_reason` и
`RateLimitEvent`; текущая обёртка сводит не все варианты к единому контракту.

## Findings

### 1. Production incident воспроизведён по журналу

**CONFIRMED — direct measurement.**

Read-only журнал Contabo за 2026-07-27:

```text
23:48:36.364 preventive compact (idle 55min, ctx 29%)
23:48:36.371 phase idle → compacting
23:48:37.361 Compact: requesting summary, before=29.0%
23:48:38.088 Compact: summary request failed:
  SDK error during summary: You've hit your monthly spend limit ...
23:48:38.104 phase compacting → idle
```

В этом интервале нет `pre-reset`/`post-reset`: конкретно при наблюдавшемся error
старый session не был сброшен. Серверная операция завершилась примерно за 1.74 с,
поэтому 11-минутное состояние в Telegram не было живым compact task.

### 2. Точный production wire variant — `AssistantMessage(error="rate_limit")`

**CONFIRMED — direct measurement + primary SDK source.**

Read-only metadata из production JSONL для synthetic assistant event
`2026-07-27T16:48:38.070Z`:

```text
top-level error = rate_limit
isApiErrorMessage = true
model = <synthetic>
content types = [text]
text contains "limit"
```

Официальный parser переносит top-level `data["error"]` в
`AssistantMessage.error` [1]. Официальный тип перечисляет
`billing_error` и `rate_limit` отдельно от normal content [2].

Текущий `ClaudeSession.send_message()` это поле не проверяет: сначала выдаёт
`TextBlock` как `{"type": "text"}`, затем `ResultMessage.result` как второй
`{"type": "error"}`. Локальный fake-stream на текущем wrapper дал:

```text
chunks = [
  {"type": "text",  "content": "You've hit your monthly spend limit ..."},
  {"type": "error", "content": "You've hit your monthly spend limit ..."}
]
cached_rate_limit = None
```

Следствие: обычный response path успевает показать raw limit как live text; compact
успевает принять тот же текст за summary до позднего error.

### 3. Есть четыре релевантных SDK surfaces, typed поля приоритетнее regex

**CONFIRMED — primary source + installed production package inspection.**

У production `claude-agent-sdk==0.2.128`:

1. `AssistantMessage.error`: `rate_limit` или `billing_error` [2].
2. `ResultMessage.is_error` + `api_error_status`; 429 явно документирован как
   безопасный классификатор API rate limit [3][4].
3. `ResultMessage.terminal_reason`; установленная версия допускает terminal reason
   для blocking limit, поэтому поле должно участвовать как дополнительный signal,
   но не как единственный.
4. `RateLimitEvent.rate_limit_info.status == "rejected"` с `resets_at`, типом окна и
   utilization; официальный docstring прямо рекомендует backoff на `rejected` [5].

Production incident не оставил `RateLimitEvent` в приложенческом логе, поэтому
опираться только на него нельзя. Для OAuth Claude Code monthly spend limit
наиболее надёжным observed signal оказался `AssistantMessage.error="rate_limit"`.

### 4. Текущий код способен сбросить сессию при потере позднего Result error

**CONFIRMED — deterministic fake-stream experiment.**

Эксперимент с `compact_session()` и единственным обычным text chunk, содержащим
monthly limit (модель варианта, где typed `AssistantMessage.error` потерян wrapper'ом,
а позднего `ResultMessage.result` нет):

```text
resets = 1
session_id = None
notices = [
  "🗜 Сжимаю контекст... (было 29%)",
  "📋 Compact summary: ... You've hit your monthly spend limit ...",
  "⚠️ Контекст сброшен, но саммари могло не загрузиться ..."
]
```

То есть safety сейчас зависит от второго error event. Typed error надо
нормализовать в `claude_session.py` до выдачи content; `compact.py` всё равно должен
иметь защитный fallback на raw text, чтобы не сбросить session при несовместимой
версии CLI/SDK.

### 5. Progress не имеет terminal update contract

**CONFIRMED — source inspection; причина именно `00:11:19` остаётся UNCERTAIN.**

`compact_session()` вызывает `notify("🗜 ...")`, а `ChatState` реализует `notify`
простым `bot.send_message()`. У progress message нет handle, edit или delete; success
и failure отправляются отдельными сообщениями. Поэтому стартовое состояние визуально
никогда не завершается.

Отдельный `ToolStatusTracker` имеет секундный ticker и иконку `📋` для YouGile, но
failed compact не создаёт этот tracker, а production interval не содержит tool event.
Следовательно, точное происхождение показанного пользователем `📋 · 00:11:19` по
доступным server artifacts не доказано. Исправление compact progress всё равно
должно быть terminal-by-construction: первый callback создаёт сообщение, terminal
callback редактирует его; при edit failure отправляется финальный fallback.

### 6. Auto-compact может делать лишний заведомо обречённый запрос

**CONFIRMED — source inspection.**

`_run_batch()` вызывает `_maybe_auto_compact()` даже после ответа, завершившегося
лимитом. При контексте выше порога это немедленно запускает второй Claude request на
summary. `ClaudeSession.rate_limit` сейчас обновляется только `RateLimitEvent`, но не
observed `AssistantMessage.error`, поэтому у auto path нет устойчивого stop signal.

Safe минимальный контракт: wrapper запоминает rejected/limit как latch завершённого
turn. Latch очищает только успешный terminal `ResultMessage` без pending typed/text
limit signal. Чужой `RateLimitEvent(status="allowed")` не очищает latch: у событий
несколько независимых `rate_limit_type` и отдельный overage scope. Auto-compact
проверяет latch до summary. Ручной/preventive compact может сделать живую проверку,
чтобы не застрять навсегда на stale monthly-limit state после изменения подписки.

### 7. Cancellation после reset сейчас теряет durable session pointer

**CONFIRMED — deterministic cancellation experiment + SDK receive contract.**

Fake compact вернул корректный summary, затем задача была отменена сразу после
`reset_async()` и до завершения preamble:

```text
session_id_after_cancel = None
notices = ["🗜 ...", "📋 Compact summary: valid summary"]
CancelledError base = BaseException
```

Текущие `except Exception` не выполняют rollback. Дополнительно установленный SDK
документирует `receive_response()` как поток до terminal `ResultMessage`; значит
typed Assistant error нельзя выдавать consumer'у как terminal до того, как wrapper
сам дочитал Result и обновил своё состояние. Оба свойства должны быть закрыты
тестами, а не только обработкой observed happy ordering.

## Recommended minimal implementation

1. В `claude_session.py` нормализовать typed SDK signals в один error chunk
   (`kind="usage_limit"`), не выдавая synthetic limit `TextBlock`; при этом **не
   yield-ить ошибку сразу из `AssistantMessage`**. Wrapper обязан запомнить typed
   error, дочитать matching terminal `ResultMessage`, сначала обновить
   `_expected_results`/session state и только затем выдать один error chunk.
   Иначе consumer закроет async generator и оставит terminal result в очереди
   persistent client. Сохранить regex fallback для старых CLI.
2. В `compact.py` на `kind="usage_limit"` или fallback raw limit немедленно вернуть
   `ok=False, reason="usage_limit"` до `reset_async()`. Короткий UI-текст:
   `⏳ Лимит Claude исчерпан — сжатие пропущено, контекст сохранён.`
3. Не отправлять `📋 summary` до успешной загрузки preamble. Замену session сделать
   durable-транзакцией внутри `ClaudeSession`: старый SID остаётся в session file,
   сохранение candidate SID временно откладывается, commit атомарно записывает новый
   SID только после успешного preamble. Любая ошибка или `BaseException`, включая
   `asyncio.CancelledError`, disconnect'ит candidate и возвращает старый SID/cache.
   Простого «стереть SID, а потом записать обратно» недостаточно: process crash или
   cancellation между этими действиями потеряет durable pointer.
4. Дать compact notify семантику `replace=True`: start создаёт progress message,
   terminal outcome редактирует его; summary остаётся отдельным сообщением только
   после полного успеха. `CancelledError` обязан пройти через shielded best-effort
   terminal update, после чего быть повторно поднят.
5. Auto-compact пропускает summary request, когда wrapper только что зафиксировал
   active rejection; только следующий успешный terminal Result без pending limit
   очищает rejection. `allowed` event другого limiter scope недостаточен.
6. В `response_stream.py` после единственного friendly limit notice выставить явный
   `terminal_handled=True` (для обычного `message.answer` и reminder
   `bot.send_message`). Epilogue отправляет `empty` только если нет text/finalized
   **и** terminal outcome не был обработан. Иначе подавление synthetic TextBlock
   детерминированно создаст дубль «лимит» + «Пустой ответ».

## Counter-evidence and rejected alternatives

- **«Текущий код уже сохраняет session при observed error».** Да: на конкретном
  инциденте поздний Result error пришёл и reset не вызвался. Это не закрывает
  variant с одним typed Assistant error; fake-stream доказал destructive fallback.
- **«Достаточно regex в `compact.py`».** Не закрывает raw text, уже показанный
  response streamer'ом, не использует status 429/typed error и дублирует
  классификацию между модулями.
- **«Навсегда блокировать compact после первого monthly limit».** Отклонено:
  observed limit был снят, и preventive compact успешно прошёл примерно через
  77 минут без промежуточного успешного обычного ответа. Stale cache не должен
  блокировать manual/preventive recovery.
- **«Убрать progress целиком».** Успешный compact реально занимает 2–3 минуты по
  production логам; отсутствие progress ухудшит UX. Нужен редактируемый terminal
  progress, а не удаление обратной связи.

## Affected files

- `claude_session.py` — typed error normalization, dedup, limit state, safe restore.
- `compact.py` — limit-safe transaction, delayed summary notification, structured
  failure reason.
- `chat_state.py` — terminal progress edit/fallback and auto-compact suppression.
- `response_stream.py` — consume normalized `kind="usage_limit"` and retain fallback
  detector without raw/duplicate Telegram output.
- `tests/` — SDK event variants, no-reset invariant, progress finalization,
  preamble rollback, auto-loop prevention.

`handlers.py` не требуется: manual `/compact` уже проходит через `ChatState`.

## Risks and edge cases

- `billing_error` не всегда означает rate limit; user-facing текст должен быть
  нейтральным «лимит/оплата Claude», но session-preservation одинаков.
- Partial genuine summary followed by limit must be discarded; reset запрещён.
- Preamble can fail after successful summary; старый SID должен всё это время
  оставаться durable, candidate не может перезаписать session file до commit.
- `asyncio.CancelledError` наследуется от `BaseException`, а не `Exception`;
  transaction cleanup и terminal progress нельзя строить только на
  `except Exception`.
- Нельзя yield-ить typed Assistant error до чтения terminal Result: закрытие
  consumer'ом async generator оставит persistent receive queue в неконсистентном
  состоянии.
- Telegram edit может упасть или получить flood control; terminal fallback обязан
  отправить короткий финальный текст и не бросать compact coroutine.
- Cached rejection must suppress только автоматический повтор, не навсегда запрещать
  manual/preventive recovery.
- Friendly limit notice должен ставить terminal-handled flag в обоих Telegram
  delivery paths; факт отдельной отправки не отражается в `parts`/`finalized` и сам
  по себе не подавляет существующий `empty` fallback.
- Empty summary без limit остаётся отдельной non-destructive ошибкой без raw stack.

## Codex adversarial review outcome

- Round 1 transport timeout не дал verdict, но partial output нашёл два blocking:
  ранний yield до terminal Result и non-transactional rollback при cancellation.
  Оба проверены по SDK source/экспериментом и приняты в findings/recommendation.
- Round 2: `REVISE`. Приняты terminal-handled flag для подавления `empty` fallback и
  очистка rejection latch только успешным terminal Result, а не произвольным
  `RateLimitEvent(status="allowed")`.
- Round 3: `APPROVED`. Оба предыдущих finding помечены `FIXED`; новых
  blocking/suggestion/question findings нет.
- Full review artifact: `docs/tasks/13/codex-review-research.md`.

## Sources

1. [Primary] Anthropic Agent SDK Python parser:
   https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/_internal/message_parser.py
2. [Primary] Anthropic Agent SDK Python types:
   https://raw.githubusercontent.com/anthropics/claude-agent-sdk-python/main/src/claude_agent_sdk/types.py
3. [Primary] Anthropic Agent SDK Python changelog:
   https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md
4. [Primary] Anthropic API error reference:
   https://platform.claude.com/docs/en/api/errors
5. [Primary] Anthropic API rate limits:
   https://platform.claude.com/docs/en/api/rate-limits
6. [Measurement] Contabo `journalctl -u kesha-bot-vps` and
   `/opt/kesha-bot/logs/kesha.log.2026-07-27`, read-only on 2026-07-28.
7. [Measurement] Production session JSONL metadata for the incident and local
   deterministic fake streams, read/run on 2026-07-28.
