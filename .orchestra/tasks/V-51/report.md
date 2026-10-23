# V-51 — DONE

Реализовано поведение из образца Orchestra 1:1:

- `media.py`: чистая `_format_transcript`, порог 0.65 (строго меньше), заголовок и `[?]` перед
  пунктуацией/закрывающими кавычками; игнорируются отсутствующая, булева и неподходящая confidence.
  При совпадении токенов сохраняется исходное форматирование; при несовпадении используются все
  слова провайдера с маркерами. `transcribe` возвращает и кэширует результат форматтера.
  URL/параметры Deepgram, включая `language=ru`, и лог длительности/стоимости/длины сохранены.
- `system_prompt.txt`: одна строка рядом с описанием голосовых объясняет маркер и уточнение при сомнении.
- `CHANGELOG.md`: patch v2.13.1.
- `tests/test_media.py`: 25 офлайн-тестов. Проверены порог и его граница, пунктуация, кавычки,
  пробелы/переносы, повторяющиеся токены, smart-format fallback, пустая расшифровка, варианты confidence,
  замоканный ответ Deepgram, сохранение/повторная загрузка кэша, старый кэш и ошибки API/JSON.

Проверки выполнялись provisioned uv 0.11.28, с PATH из задания. Версии отдельно подтверждены:
`mcp==1.28.1`, `claude-agent-sdk==0.2.158`. Временные каталоги, медиа и логи направлялись внутрь
каталога задачи; сырые логи и временные файлы удалены из результата.

Команды и результаты:

```sh
uv run --frozen python -m pytest tests/test_media.py -q
```

Не выполнена как проектная проверка: в копии нет `pyproject.toml` и `uv.lock`;
запуск завершился с `No module named pytest`. Сам provisioned uv доступен.
Lock/project-файлы и окружение `.venv` не создавались и не подменялись.

```sh
uv run --isolated --no-project --exclude-newer 2030-01-01 --with-requirements requirements.txt --with mcp==1.28.1 --with claude-agent-sdk==0.2.158 --with pytest --with pytest-asyncio python -m pytest tests/test_media.py -q
```

**25 passed**.

```sh
uv run --isolated --no-project --exclude-newer 2030-01-01 --with-requirements requirements.txt --with mcp==1.28.1 --with claude-agent-sdk==0.2.158 --with pytest --with pytest-asyncio python -m pytest -q tests
```

Остановка при collection: `tests/test_compact_evaluator.py` импортирует
`scripts.evaluate_compact_prompt`, а каталог `scripts` отсутствует в выданной копии
(`ModuleNotFoundError: No module named 'scripts'`). Полный прогон остаётся оркестратору.

Та же команда с `--ignore=tests/test_compact_evaluator.py`: **669 passed, 3 skipped, 3 failed**.
Падения:

- `tests/test_kesha_mcp_proxy.py::test_stdio_proxy_sends_file_to_bound_telegram_chat`
  и `tests/test_tool_bridge.py::test_socket_is_owner_only`: `OSError: AF_UNIX path too long`.
  Абсолютный путь временных файлов внутри этой копии превышает лимит Unix-сокета.
- `tests/test_safety_refusal.py::test_refused_turn_is_cut_and_next_turn_resumes_the_fork`:
  SDK не нашёл сессию в project directory на длинном временном пути.
  Повтор на тех же пинах с `--basetemp=.orchestra/tasks/V-51/.run/t`: **1 passed**.
  Это свидетельствует о зависимости результата от длины пути; код восстановления не изменялся.

Наблюдения для передачи:

- Старые записи кэша содержат только строку, без word confidence, и возвращаются как раньше.
  Разметка появляется при новом запросе Deepgram; восстановить её из старого кэша невозможно.
- При smart format fallback вывод может отличаться от форматированного transcript (например,
  словами вместо числа). Это намеренное поведение предоставленного образца: сохраняются все
  слова и относящиеся к ним маркеры.
- В этой глубокой копии стандартный pytest tmp_path мешает проверкам Unix-сокетов и SDK.
  Для полного прогона нужен более короткий путь окружения и отсутствующий модуль scripts.
- Живой Deepgram/Telegram и деплой не проверялись; HTTP в новых тестах замокан.
