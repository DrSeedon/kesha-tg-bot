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

## Production deployment evidence

Deployment выполнен 2026-07-28 на Contabo `kesha-bot-vps` после merge
`origin/main=3567de21bf4e75b039e049ce3144b67ddddd8ca5`.

### Rollback и сохранение production state

- Pre-deploy HEAD:
  `19a123684144da7c8d14e2ab6132ba616e451092`.
- Rollback snapshot:
  `/var/backups/kesha-bot/task13-20260728T072854Z`.
- Snapshot содержит прежний HEAD, `.env`, session archive, pre-deploy session
  hashes, service metadata и `prod-dirty.patch`.
- Единственная существовавшая dirty-правка `claude_session.py`
  (`thinking={"type": "adaptive"}`, `effort="high"`) сохранена через stash,
  автоматически применена поверх нового main без конфликта и осталась единственной
  строкой в `git status`.
- Recoverable stash сохранён:
  `stash@{0}: task13-deploy-20260728T072854Z`.
- Оба production session-файла прошли `sha256sum -c` до и после pull, restart и
  live OAuth smoke: `OK`; реальные SID не читались и не изменялись.

### Package и static smoke

```text
HEAD=3567de21bf4e75b039e049ce3144b67ddddd8ca5
IMPORT_OK
claude-agent-sdk=0.2.128
bundled Claude Code=2.1.220
py_compile=OK
```

На production venv нет `pytest`; зависимости туда ради проверки не
доустанавливались. Основанием остаётся полный pre-deploy suite `86 passed`, а
deployed modules дополнительно проверены standalone quota-free smoke.

Quota-free fake compact на фактическом production checkout:

```text
LIMIT_SAFE_SMOKE=OK
SID_PRESERVED=OK
TERMINAL_NOTICE=OK
```

Сценарий дал partial summary + normalized usage limit и подтвердил:

- replacement transaction завершилась rollback;
- исходный temp SID остался одинаковым в памяти и на диске;
- `📋` не отправлялся;
- progress получил terminal replacement.

### Controlled restart

```text
restart mark=2026-07-28T07:30:34Z
old PID=780
new PID=2827
systemd ActiveState=active
systemd SubState=running
```

Startup evidence:

```text
2026-07-28 14:30:46,628 [kesha] INFO Kesha bot |
CWD=/opt/cog-second-brain | Model=claude-opus-5
```

`journalctl -p err` после restart не вернул записей.

### Live OAuth / model / resume smoke

Изолированный SDK smoke запускался от production user без proxy, вне bot session
storage. Первый turn сохранил тестовый nonce, второй создал новый client с
`resume=<first session_id>` и восстановил nonce.

```text
LIVE_MODEL_1=claude-opus-5
LIVE_MODEL_2=claude-opus-5
OAUTH_TURN=OK
OAUTH_RESUME=OK
SESSION_ID_LENGTH=36
```

Реальный production compact и исчерпание quota намеренно не форсировались:
это изменило бы пользовательский контекст и потратило лимит. Limit-safe поведение
подтверждено focused regression tests и quota-free deployed-module smoke выше.

### Final production state

- Service: `active/running`, PID `2827`.
- Deployed HEAD: `3567de21bf4e75b039e049ce3144b67ddddd8ca5`.
- Model: `claude-opus-5`.
- OAuth request/resume: passed.
- Existing dirty adaptive-thinking patch: preserved.
- Production session hashes: unchanged.
- Rollback snapshot/stash: retained.
