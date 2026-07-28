# #13 — Limit-safe compact

## Goal

Исправить observed Claude Code OAuth usage-limit path без нового error framework:
нормализовать существующие SDK events в `ClaudeSession`, сделать compact
транзакционным на существующей session boundary и гарантировать один terminal UI
outcome через `ChatState`.

Прод и deployment в этой фазе не затрагиваются.

## Assumptions

- Production остаётся на `claude-agent-sdk==0.2.128` / bundled CLI 2.1.220.
- `AssistantMessage.error in {"rate_limit", "billing_error"}`,
  `ResultMessage.api_error_status == 429`, `terminal_reason == "blocking_limit"` и
  существующий text regex — варианты одного non-retryable user-facing outcome.
- `RateLimitEvent(status="rejected")` обновляет telemetry, но не является сам по
  себе terminal result: wrapper дочитывает matching `ResultMessage`.
- Ручной `/compact` может сделать живую попытку после изменения лимита. Оба
  автоматических пути — threshold auto-compact и preventive idle compact — пропускают
  попытку, пока limit latch не снят успешным terminal turn.

## Implementation

### `claude_session.py`

1. Перенести существующий узкий detector usage/session limit из
   `response_stream.py` в SDK boundary как маленькую функцию; сохранить regex
   fallback для старых/неполных CLI events.
2. В `ClaudeSession.send_message()`:
   - держать limit signal отдельно для каждого ожидаемого `ResultMessage` и общий
     `limit_seen` для всего receive batch; при `AssistantMessage.error`
     `rate_limit`/`billing_error` помечать следующий Result и не выдавать synthetic
     `TextBlock`;
   - при `RateLimitEvent` продолжать обновлять `rate_limit`, но не очищать latch по
     чужому `allowed` event;
   - при `ResultMessage` объединить pending typed signal,
     `api_error_status == 429`, `terminal_reason == "blocking_limit"` и text
     fallback;
   - после каждого Result сбрасывать только его pending state; несколько limit
     Results в injected batch схлопывать в один normalized outcome после полного
     drain, а последующий success не должен стереть общий `limit_seen`;
   - переход «последний Result получен → injections закрыты» сделать атомарным под
     `_query_lock`: уменьшить `_expected_results`, выставить `_is_processing=False`
     при нуле и только после освобождения lock выдать один
     `{"type": "error", "kind": "usage_limit", ...}`;
   - `inject()` повторно проверяет `_is_processing` уже внутри `_query_lock`.
     Если injection выиграл race, он сначала увеличивает `_expected_results` и
     receive loop дочитывает его Result; если terminal transition выиграл race,
     injection отклоняется. Следующий query не может получить старый Result;
   - только полностью успешный terminal batch без `limit_seen` очищает
     `usage_limit_active`; любой limit result выставляет latch.
3. Добавить три узких operation для замены session:
   `begin_session_replacement()`, `commit_session_replacement()` и
   `rollback_session_replacement(snapshot)`.
   - snapshot содержит старые SID/context cache и явное transaction state;
     begin временно запрещает обычный `_save_session`, отключает старый client от
     active slot и начинает candidate, не меняя старый session file;
   - существующую запись одного SID сделать crash-atomic: temp file в том же
     каталоге, flush/close и `os.replace`; in-memory candidate становится active
     только после успешного replace;
   - commit после успешного preamble атомарно записывает candidate SID и переводит
     transaction state в `committed`, тем самым запрещая последующий rollback;
   - rollback до commit disconnect'ит candidate, восстанавливает старые SID/cache
     и оставляет limit latch от неудачной попытки; после commit это no-op;
   - rollback вызывается на любой `BaseException`, включая cancellation, только
     пока transaction остаётся pre-commit.

Нового общего exception hierarchy/dispatcher не добавлять.

### `response_stream.py`

1. Использовать normalized `chunk["kind"] == "usage_limit"` первым, text detector
   оставить fallback.
2. В обоих delivery paths — обычный `message` и reminder через `_bot` — отправить
   один локализованный короткий limit notice и выставить `terminal_handled=True`.
3. Limit может прийти после уже показанного `StreamEvent(text_delta)`. В terminal
   branch очистить `parts`/delta buffer и заменить текущий live message тем же
   friendly notice. Если edit не удался — best-effort удалить live message и
   отправить ровно один fallback. Отдельное второе notice не отправлять.
4. Перед terminal outcome завершить `ToolStatusTracker`.
5. `empty` fallback выполнять только когда нет text/finalized и
   `terminal_handled == False`; raw limit text не добавлять в `parts`, message log
   или live edit.

### `compact.py`

1. Summary stream дочитывать до terminal result. Pending `usage_limit` или raw-text
   fallback делает результат `ok=False, reason="usage_limit"`; partial text
   отбрасывается, session replacement не начинается.
2. После непустого успешного summary начать `ClaudeSession` replacement transaction,
   отправить preamble в candidate и:
   - commit только после terminal success и наличия candidate SID;
   - rollback на SDK error, limit, empty/invalid result или `BaseException` только
     до commit;
   - сразу после успешного atomic commit пометить transaction committed. Ошибка
     summary notification/context usage или cancellation после этой точки не
     восстанавливает старый SID;
   - cancellation до commit после shielded rollback и terminal progress update
     повторно поднять; после commit shielded cleanup/terminal update также
     повторно поднимает cancellation, сохраняя candidate.
3. `📋 Compact summary` отправлять отдельными частями только после commit. Ни limit,
   ни generic failure не отправляют `📋`.
4. Все ошибки показывать коротко без raw SDK/stack:
   `⏳ Лимит Claude исчерпан — сжатие пропущено, контекст сохранён.`
   Generic failure также явно говорит, что контекст сохранён.
5. `maybe_auto_compact()` до summary проверяет `usage_limit_active` и возвращает
   structured skip без второго Claude request и без второго Telegram notice.

### `chat_state.py`

1. Один маленький notifier closure на `ChatState` хранит handle progress message:
   - первый `replace=True` создаёт `🗜 ...`;
   - следующий `replace=True` редактирует тот же message в terminal success/failure;
   - обычный notify отправляет committed `📋 summary` отдельно;
   - если edit не удался, отправляет terminal fallback и best-effort удаляет старый
     progress message.
2. Использовать notifier и в `_do_compact()`, и в `_maybe_auto_compact()`.
3. Сохранить provenance deferred compact без новой state machine:
   `request_compact(automatic=False)` записывает рядом с existing
   `compact_requested` один boolean automatic. Preventive path передаёт
   `automatic=True`; manual `/compact` сохраняет default `False`. При coalescing
   нескольких deferred requests manual provenance sticky до consumption:
   automatic request не может перезаписать уже сохранённый `False`.
4. Проверять `usage_limit_active` и до automatic request, и в deferred execution
   boundary `_finish_processing()`/`_do_compact()`. Если текущий user turn поставил
   latch уже после preventive check, automatic request снимается без compact;
   manual request latch не блокирует.
5. Existing `finally` phase restoration/drain сохранить: failure, limit и
   cancellation не оставляют `COMPACTING`.

### Tests

Добавить focused tests без сетевых/production вызовов:

- `tests/test_claude_session_limit.py`
  - exact production sequence:
    `AssistantMessage(error="rate_limit", TextBlock(raw))` +
    `ResultMessage(is_error=True, result=raw)`;
  - fake variant: typed Assistant limit + terminal Result без позднего result error;
  - 429 и `blocking_limit` variants;
  - race `inject()` против последнего Result: либо injected Result дочитан, либо
     injection отклонён; следующий query не получает stale Result;
  - mixed batch с `_expected_results > 1`: limit→success, success→limit и несколько
     limits дают один normalized outcome, per-Result state не течёт;
  - `rejected(A) → allowed(B) → terminal error` не очищает latch;
  - только полностью успешный terminal batch очищает latch.
- `tests/test_compact_limit.py`
  - summary limit и partial-summary-then-limit не вызывают session replacement,
    session file/SID/context остаются прежними;
  - restart-style проверки session file: crash до begin, во время candidate и
     failed `os.replace` загружает старый полный SID; completed commit загружает
     новый полный SID;
  - cancellation после begin/reset candidate вызывает rollback старого SID и
     re-raises `CancelledError`; cancellation сразу после commit сохраняет
     candidate SID в памяти и на диске;
  - preamble limit/error тоже rollback'ит старый SID и не отправляет `📋`;
  - success commit сохраняет новый SID, затем отправляет summary;
  - progress terminalized через edit; edit failure использует fallback/delete.
- `tests/test_response_limit.py`
  - sequence `StreamEvent(raw delta) → AssistantMessage(rate_limit) →
     ResultMessage` в обычном и reminder path заменяет уже показанный raw live
     message ровно одним friendly notice;
  - нет raw error, `📋` и `empty`;
  - существующий tool status finalize вызывается.
- `tests/test_preventive_compact.py`
  - preventive latch skip не вызывает compact;
  - preventive check→deferred request race, где user turn затем ставит latch, не
     вызывает compact в execution boundary;
  - overlapping manual→preventive requests сохраняют manual provenance и не
    отбрасываются из-за latch;
  - после успешного terminal turn latch снят и automatic path снова разрешён.

## What not to touch

- `handlers.py`: manual `/compact` уже идёт через `ChatState.request_compact()`.
- Тексты system prompt, модель, session file format, ChatPhase topology.
- Retry policy обычных transient session/process errors.
- Production `.env`, systemd и Contabo deployment.

## Verification

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q \
  tests/test_claude_session_limit.py \
  tests/test_compact_limit.py \
  tests/test_response_limit.py \
  tests/test_preventive_compact.py \
  tests/test_session_limit.py

UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q
```

Перед тестами локальная среда должна удовлетворять `requirements.txt`
(`claude-agent-sdk>=0.2.128`), чтобы typed fields production SDK были реально
доступны.

## Migration and rollback

Миграции данных нет. Session files сохраняют прежний формат — один SID.

Code rollback возвращает старую логику без конвертации данных. Prod deploy/restart
делается только отдельным разрешением после merge.

## Tickets

### T1 — Typed SDK limit → один terminal Telegram outcome

- Files: `claude_session.py`, `response_stream.py`,
  `tests/test_claude_session_limit.py`, `tests/test_response_limit.py`,
  `tests/test_session_limit.py`
- AC:
  - observed production `AssistantMessage(error="rate_limit")` + error Result
    даёт один normalized usage-limit chunk без raw text;
  - variant без позднего Result error всё равно распознаётся, но terminal Result
    дочитан до выдачи outcome;
  - 429/`blocking_limit` распознаются;
  - injection-vs-terminal race не оставляет stale Result; mixed multi-Result
     batches используют per-Result state и схлопывают limits в один outcome;
  - latch не снимается чужим `allowed` event/поздним success после limit и снимается
     только полностью успешным terminal batch;
  - raw delta, показанный до typed limit, заменяется friendly terminal message;
  - обычный и reminder Telegram paths отправляют ровно одно friendly сообщение,
    не отправляют raw/`empty`, status ticker завершён.
- blocked-by: none

### T2 — Transactional compact + terminal progress + automatic latch

- Files: `claude_session.py`, `compact.py`, `chat_state.py`,
  `tests/test_compact_limit.py`, `tests/test_preventive_compact.py`
- AC:
  - summary limit, partial summary + limit и empty summary не меняют SID/session
    file/context и не вызывают commit;
  - crash/restart до begin, во время candidate и при failed atomic replace видит
     старый целый SID; completed commit видит новый целый SID;
  - cancellation после session reset/begin replacement восстанавливает старый SID;
     cancellation сразу после commit не откатывает новый SID; обе re-raise
     cancellation;
  - preamble limit/error rollback'ит старый SID; ни один failure path не отправляет
     `📋`;
  - successful terminal summary+preamble commit'ит новый SID до показа `📋`;
  - progress message всегда получает terminal edit либо fallback/delete;
  - threshold auto и preventive compact не повторяют summary при active latch;
     deferred preventive request повторно проверяет latch после racing user turn;
     coalesced manual request имеет sticky приоритет над preventive;
     successful terminal turn снимает latch; manual compact остаётся доступен.
- blocked-by: T1
