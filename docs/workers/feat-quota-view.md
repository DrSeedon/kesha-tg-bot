# feat-quota-view — личные заметки

## Окружение kesha-tg-bot на VPS: своего venv в репозитории НЕТ
Ни в worktree, ни в `/home/kesha/projects/kesha-tg-bot`. `/opt/kesha-bot/.venv` — боевой, без
pytest, не трогать. Задание может писать «прогон `.venv/bin/python -m pytest`» так, будто venv
существует, — его надо создать самому:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --exclude-newer 2030-01-01 \
    -r requirements.txt pytest pytest-asyncio aiohttp
```

`--exclude-newer 2030-01-01` обязателен: глобальный `~/.config/uv/uv.toml` держит
`exclude-newer = "7 days"`, из-за чего `claude-agent-sdk>=0.2.128` не резолвится
(«only claude-agent-sdk<=0.2.114 is available»). `.venv/` в `.gitignore`, коммиту не мешает.

Смоук `python -c "import bot"` падает на `validate_token`, если в окружении нет
`TELEGRAM_BOT_TOKEN`. Это НЕ ошибка кода — до `Bot()` все импорты уже отработали. Проверять с
фейковым токеном: `TELEGRAM_BOT_TOKEN=123456:AAfake python -c "import bot"`.

## Утверждение «два таймаута складываются» проверяется достижимостью веток
Ревьюер сложил 30 с (`read_quota`) и 10 с (HTTP) в 40 с. Оба метода квоты (`read_quota`,
`quota_summary`) определены ТОЛЬКО в `codex_session.py` — на Codex сети нет, на Claude нет
`read_quota`. Прежде чем принимать находку про суммарную латентность, грепнуть определения и
спросить, выполняются ли обе ветки в одном вызове.
