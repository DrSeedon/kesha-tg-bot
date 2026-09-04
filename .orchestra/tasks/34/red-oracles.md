# #34 — Frozen RED oracle evidence

**Baseline commit:** `58c3f31`

All commands use the current `claude-agent-sdk==0.2.152` environment and fail by
assertion for missing product behavior; there are no import or collection errors.

## T1

```text
command: /home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil --with telegramify-markdown python -m pytest -q --tb=short tests/test_auto_compact_admission.py -k test_t1
exit: 1
summary: 7 failed, 4 deselected
first assertion: E AssertionError: the admitted 79% batch was not sent
```

## T2

```text
command: /home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil --with telegramify-markdown python -m pytest -q --tb=short tests/test_auto_compact_admission.py -k test_t2
exit: 1
summary: 1 failed, 10 deselected
first assertion: E AssertionError: Codex admission did not compact
```

## T3

```text
command: /home/kesha/.local/bin/uv run --exclude-newer 2030-01-01 --isolated --no-project --with pytest --with pytest-asyncio --with aiogram --with aiogram-media-group --with 'claude-agent-sdk==0.2.152' --with python-dotenv --with python-dateutil --with telegramify-markdown python -m pytest -q --tb=short tests/test_auto_compact_admission.py tests/test_response_limit.py tests/test_activity_ingress.py -k test_t3
exit: 1
summary: 7 failed, 1 passed, 30 deselected
first assertion: E AssertionError: the independent night scheduler still armed
```

## Broader focused baseline

```text
exit: 1
summary: 22 failed, 217 passed, 3 skipped in 11.53s
```

The additional 12 failures are revised pre-existing assertions for the same missing
behavior: Claude 92% pressure, Codex 90% pressure/unknown fields, removal of manual
context-limit UX/latch, and the new bilingual failure key.
