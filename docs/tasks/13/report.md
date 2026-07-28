# #13 — Limit-safe compact: implementation report

## Result

Claude OAuth usage/rate limit теперь завершается как один typed terminal outcome:
без raw limit/`empty`/`📋` дублей, без retry и без зависшего progress. Compact не
меняет durable session ID до успешных summary + candidate preamble и атомарного
commit; любое pre-commit исключение или cancellation восстанавливает исходную
сессию.

Production не изменялся и не перезапускался.

## Tickets

### T1 — Typed SDK limit → один terminal Telegram outcome

- `ClaudeSession.send_message()` объединяет `AssistantMessage.error`,
  `ResultMessage` 429/`blocking_limit`, `RateLimitEvent(rejected)` и text fallback
  в один `kind="usage_limit"` после полного terminal drain.
- Per-Result/stream latch не очищается чужим `allowed` либо success после limit;
  полностью успешный terminal batch снимает latch.
- `inject()` атомарно конкурирует с terminal Result и повторно проверяет client
  после awaited query; failed injection возвращает `False`, поэтому ChatState
  может безопасно requeue сообщение.
- Response layer заменяет уже показанный raw delta тем же friendly terminal
  сообщением для обычного message и reminder path, завершает tool status и
  подавляет `empty`.

### T2 — Transactional compact + progress + provenance latch

- SID persistence использует same-directory temp + `fsync` + `os.replace`.
- Replacement transaction защищает старый SID до summary, не допускает durable
  `_invalidate_session()` при retry и проверяет, что summary завершился в той же
  source session.
- Candidate отделяется только после валидного summary; commit disarms rollback.
  Cancellation до commit сохраняет старый SID, после commit — новый.
- Compact progress создаётся один раз и получает terminal edit; при edit failure
  старое сообщение удаляется best-effort и отправляется один fallback.
- Automatic threshold/preventive compact пропускается при active limit latch.
  Deferred automatic request повторно проверяет latch, а manual provenance имеет
  sticky приоритет при coalescing.

## Files

- `claude_session.py` — typed limit normalization, injection gate, atomic SID
  persistence/replacement transaction.
- `response_stream.py` — единый friendly terminal limit outcome.
- `compact.py` — non-destructive summary/candidate transaction и terminal notify.
- `chat_state.py` — progress notifier и automatic/manual provenance.
- `tests/test_claude_session_limit.py` — CLI 2.1.220, multi-Result и injection races.
- `tests/test_response_limit.py` — message/reminder raw-delta replacement.
- `tests/test_compact_limit.py` — limit, cancellation, crash/restart и SID atomicity.
- `tests/test_preventive_compact.py` — latch/provenance/progress races.

Implementation diff относительно approved plan commit:
`1225 insertions, 166 deletions`; основная добавленная масса — focused regression
tests.

## Verification

Focused:

```text
39 passed in 3.28s
```

Команда:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q \
  tests/test_claude_session_limit.py \
  tests/test_response_limit.py \
  tests/test_session_limit.py \
  tests/test_compact_limit.py \
  tests/test_preventive_compact.py
```

Full suite:

```text
86 passed in 14.08s
```

Команда:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m pytest -x -q
```

Обязательные сценарии покрыты отдельными тестами:

- observed CLI 2.1.220 `AssistantMessage(error="rate_limit")`;
- typed limit без позднего Result error;
- injection/terminal и in-flight-query/stream-failure races без stale Result/loss;
- cancellation до и сразу после SID commit;
- crash/restart до candidate, failed `os.replace`, completed commit;
- summary retry с `_invalidate_session()` не меняет persisted source SID;
- ровно один terminal Telegram outcome для message/reminder;
- deferred automatic latch race и manual-over-preventive coalescing.

## Codex implementation review

Первый содержательный раунд нашёл два blocking:

1. revalidation `inject()` после awaited query;
2. защита source SID должна начинаться до summary.

Оба проверены по коду, исправлены и закрыты новыми regression tests. Round 2:

```text
Prior blocker 1 — FIXED
Prior blocker 2 — FIXED
New blocking findings — none
Verdict — APPROVED
blocking findings: 0
```

Полный review: `docs/tasks/13/codex-review-impl.md`.

## Breaking / migration / deployment

- Breaking changes: none.
- Session file format: unchanged, один SID.
- Data migration: none.
- Production deployment/restart: not performed; требуется merge/approval.
