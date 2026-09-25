# V-38 — переполнение контекста: причины, изменения, проверка

## Причины (с доказательствами)
1. **Переполнение показано как лимит подписки.** Прод-лог 25.09 (`prod-0925-excerpt.log`): `Result: 0.1s, 1 turns, stop=stop_sequence, cost=$0.0000`, затем `usage_limit`. В CLI 2.1.280 (`strings` бинаря `_bundled/claude`) query-loop при `level==="blocked"` (`e >= s-3000`) отдаёт `Ro({content:"Prompt is too long", error:"invalid_request"})` и `return {reason:"blocking_limit"}`. `claude_session.send_message` считал `terminal_reason=="blocking_limit"` типизированным лимитом → `limit_seen` → `kind=usage_limit`, ветка `context_limit` проигрывала. Из-за этого же компакт вернул `usage_limit`, admission «отправил всё равно», ответ — «лимит подписки».
2. **Наш компакт не помещается в переполненную сессию** (summary-запрос отбивается тем же охранником). Родной `/compact` CLI работает: прод 25.09 руками 979153→2719; через SDK проверено `probe_compact.out` (сырой клиент) и `live_native_compact.out` (`ClaudeSession.native_compact`: pre 18805→post 1924, следующий ход ок).
3. **Почему дорос до 979K.** Превентивного/ночного компакта в коде нет (убраны в #34, CLAUDE.md устарел). Единственный владелец — admission перед батчем при 95% (950K), стена CLI ≈977K → запас ≈27K. Компакты на проде 20.09 и 22.09 отработали (95%→6%); 25.09 в 14:54 один ход (4 turns, веб-инструменты) перепрыгнул порог и стену, следующий замер был уже 98%. Точный размер прироста хода в логах не виден (нет замера до хода).
4. **maxOutputTokens**: Opus 5.5 отдаёт 128000, константа была 64000 → ложный `ERROR Unexpected terminal model usage` и латч на каждом ответе (прод 14:56:16). Латч диагностический (не гейтит), но шумит и врёт.

## Изменения
- `claude_session.py`: `result_overflow` (текст/`context_limit`/виденный `context_limit`) побеждает `blocking_limit`; `expected_max_output_tokens(model)` по таблице (opus-5-5→128000, иначе 64000), свойство `ClaudeSession.expected_max_output_tokens`; `native_compact()` — `/compact` через клиент, успех только по `compact_boundary`.
- `compact.py`: `_native_compact_fallback` по `failure_reason=="context_limit"` (и только), после отката транзакции; терминал при провале: «Контекст заполнен, сжать не удалось… нужен /clear» (без слова «лимит»).
- `config.py`: `AUTO_COMPACT_TRIGGER_PCT` 95→88 (запас ≈89K).
- `chat_state.py`: после отработавшего хода (`_finish_processing(check_context=True)`) `get_context_usage()` ≥ порога → автоматический компакт. Не срабатывает после упавшего admission (иначе повторный компакт на каждое сообщение — поймано тестами `test_t1_failure_is_one_bounded_terminal…`).
- Исходное сообщение продолжается тем же батчем: путь admission уже так устроен (`_run_batch` → `compact ok` → повторный замер → `_ask_fn`).

## Тесты (`tests/test_context_overflow_recovery.py`, в коммите)
Мутации (каждая краснит тесты, файлы восстановлены): M1 вернуть `blocking_limit`=квота → 2 красных; M2 хардкод 64000 → 2; M3 убрать люк → 2 (нет отправки исходного батча / нет «/clear»); M4 люк по любой ошибке → 2; M5 игнорировать `compact_boundary` → 1; M6 убрать проверку после хода → 1.
Полный сьют на версиях прода (`--with mcp==1.28.1 --with claude-agent-sdk==0.2.158`, `--exclude-newer 2030-01-01` нужен: глобальный пин uv не видит 0.2.158): **658 passed, 3 skipped**. Изменены существующие: `test_auto_compact_admission` (триггер — запас ≥80K, а не «==95»), `test_compact_prompt` (стаб `native_compact`).

## Осталось / вне объёма
- Ход, упавший `context_limit` не в компакте (а в основном запросе), по-прежнему не повторяется (безопасность replay — #34 gap); на практике admission 88% + проверка после хода это состояние предотвращают.
- Нативная сводка не несёт наш дословный хвост и редакцию секретов.
- Деплой не делался (нужен рестарт бота; Python). CLAUDE.md про таймеры устарел — в TODO.
