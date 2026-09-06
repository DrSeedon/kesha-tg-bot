# feat-reload-cli — личная память

## Окружение (kesha-tg-bot на VPS)

- Своего venv у воркера нет. Быстрые прогоны — `/home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest`
  (главный чекаут, интерпретатор читается, worktree не трогается). Полный сьют — только
  прод-версиями через `uv run --exclude-newer 2030-01-01 --isolated --no-project
  --with-requirements requirements.txt --with 'mcp==1.28.1' --with 'claude-agent-sdk==0.2.128'`.
- `import bot` в тестах НЕВОЗМОЖЕН: на импорте создаётся `Bot(token=TOKEN)` и тянется `rag`.
  Поэтому всё, что нужно протестировать, из `bot.py` выносится в `config.py` (чистый stdlib,
  тесты его уже импортируют). Смоук вручную: `env -u HTTPS_PROXY TELEGRAM_BOT_TOKEN=1:x
  KESHA_NO_FILE_LOG=1 python -c "import bot"`.

## Как устроен этот проект (то, что пришлось выяснять)

- Команда бота = 5 мест: `handlers.h_*` + `register(dp)` + `COMMANDS_RU` + `COMMANDS_EN` +
  ключи в `config.STRINGS` (ru и en) + строка в `STRINGS["help"]` обоих языков. Забудешь help —
  `/help` начинает врать.
- Образец для любой команды, трогающей жизненный цикл сессии, — `ChatState.switch_runtime`:
  гейт по фазе под `self._lock`, удержание чата через `phase = PROCESSING`, возврат dict-результата,
  рендер в handlers, освобождение очереди через `_drain_or_idle(record_activity=False)` в
  `_finish_cancel_safely`. Не изобретать свою схему.
- Зависимости в `ChatState` приходят функциями из `ChatRegistry` (`build_runtime_fn`,
  `reset_runtimes_fn`, `reload_mcp_fn`) — ChatState не знает про реестр.

## Грабли

- Ветка ответвляется от ЛОКАЛЬНОГО main, а он отстаёт от `origin/main`. `git diff origin/main`
  показывает чужие коммиты как МОИ удаления — это не откат, а устаревшая база. Проверять
  `git log <base>..origin/main` и `git diff --stat <base>` (только свои файлы).
  Отсюда: не редактировать верх `CHANGELOG.md` из старой базы, если main туда только что писал —
  гарантированный конфликт. Текст записи класть в `.orchestra/tasks/<id>/changelog-entry.md`.
