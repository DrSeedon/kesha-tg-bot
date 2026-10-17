# Kesha TG Bot

Telegram-бот на `ClaudeSDKClient` (официальный `claude-agent-sdk`, Claude Code CLI) со вторым, переключаемым вручную рантаймом на Codex CLI. Для кого: семья владельца (два пользователя, личные чаты), один прод-узел без фейловера. Один чат = одна постоянная сессия агента на рантайм. Рабочая директория бота (`WORK_DIR`) — каталог заметок владельца с его `CLAUDE.md` и `.mcp.json`. Пользовательское описание возможностей — `README.md` (en) и `README.ru.md`; история изменений — `CHANGELOG.md`; открытые хвосты — `TODO.md`.

Этот файл — ядро правил (меньше 16 KiB, схема владельца от 17.10.2026): суть, мелкие всегда нужные правила и реестр тематических файлов. `CLAUDE.md` — симлинк на него, не копия: Codex читает AGENTS.md, Claude SDK — CLAUDE.md. Всё, что нужно только иногда, лежит в `.orchestra/kb/rules-*.md` (не в `.claude/rules/`: оттуда Claude Code грузит всё в контекст). Хроника и состояние на дату сюда не пишутся.

## Устройство

```
Telegram (aiogram 3) → handlers.py → chat_state.py (ChatState) → runtime_registry → claude_session.py → Claude Code CLI
                                          ↓                                       → codex_session.py → codex app-server
                                  response_stream.py → правка ответа в Telegram     MCP-тулы: kesha_tools.py (в процессе / через tool_bridge.py)
```

| Файл | Что делает |
|---|---|
| `bot.py` | Старт, singleton-лок, связка модулей, цикл напоминаний, inbox-сервер |
| `config.py` | Окружение, логи, строки ru/en (`STRINGS`, `t()`), загрузка MCP-серверов |
| `handlers.py` | Хендлеры сообщений и команд, меню команд |
| `chat_state.py` | Автомат чата, дебаунс, steer посреди хода, допуск по контексту, компакт, смена рантайма, фоновые ходы |
| `response_stream.py` | `_ask()`: стриминг правками, бюджет правок на чат, повторы, терминальные уведомления |
| `tool_status.py`, `telegram_io.py`, `media.py` | Пузырь тулов, помощники Telegram, загрузки и расшифровка (Deepgram) |
| `runtime_protocol.py`, `runtime_registry.py` | Узкий контракт рантайма и реестр, падающий при сборке, если бэкенд врёт о возможностях |
| `claude_session.py`, `codex_session.py` | Два бэкенда |
| `compact.py` | Компакт Claude: записка из 10 разделов → проверка → атомарная замена сессии |
| `kesha_tools.py`, `tool_bridge.py`, `kesha_mcp_proxy.py` | MCP-тулы, unix-мост для Codex (токен, сессия на чат), его stdio-запускатель |
| `memory_search.py` | Текстовый поиск по истории чата (с V-45; эмбеддингов нет) |
| `reminders.py`, `message_log.py`, `file_access.py`, `limits.py`, `quota_gate.py`, `inbox_server.py` | Напоминания (SQLite), журнал сообщений и расхода, белый список исходящих файлов, `/limits` из Orchestra, проверка квоты, локальный HTTP-inbox |
| `system_prompt.txt` | Контекст Telegram, форматирование и правила памяти для агента |

**ChatState** — центр состояния чата: `IDLE → COLLECTING → (WAITING_MEDIA) → PROCESSING → IDLE`, плюс `COMPACTING` и `STOPPING`. Вся мутация состояния чата только через его API (`accept_entry`, `request_stop`, `request_clear`, `request_compact`, `switch_runtime`, `reload_cli`, `set_debounce`); глобальных dict/set нет.

**Путь сообщения:** хендлер → `PendingEntry` → `accept_entry()` → дебаунс (по умолчанию 3 с) → пачка → допуск по контексту (при ≥88 % окна на Claude, ≥90 % на Codex сначала компакт) → `_ask()`. Сообщение посреди хода на Claude вливается в идущий ход (`steer`, V-44); на Codex ждёт следующей пачки. Сессии: `./storage/sessions/<chat_id>` (+ `.codex`), переживают рестарт.

**MCP-тулы** (`mcp__kesha__*`): `set_debounce`, `toggle_debug`, `get_bot_status`, `restart_bot`; `send_photo/file/video/audio/voice` (путь только из белого списка); `react`; `create/list/update/cancel_reminder`; `search_memory`, `read_messages`; `run_on_laptop` (только Claude, через мост не отдаётся). Заметки ищет сам агент через `rg`/Grep.

## Постоянные правила (каждое с датой и причиной)

Деплой и прод
- **Прод — один узел, деплой делает оркестратор или владелец, не воркер** (#24, 03.08.2026). Шпаргалка — `rules-prod-ops.md`, адреса вне git в `/home/kesha/projects/kesha-tg-bot/.orchestra/inventory.local.md`.
- **Перед деплоем сверять SHA на remote** (`git ls-remote origin refs/heads/main`): локальный merge без push для прода не существует, `git pull` выкатит старый код и тихо рестартнёт сервис (11.08.2026, #6).
- **Перед `merge_worker` — `git fetch && git log main..origin/main`**: он льёт в ЛОКАЛЬНЫЙ main. 11.08.2026 локальный main стоял на `a7cb359`, а прод и origin — на `1389889`; мерж #6 откатил бы работу по Codex-тулам. Лечится cherry-pick сквош-коммита на свежую ветку от `origin/main` + полный сьют на объединённом состоянии, не rebase старой ветки. Сверять именно ветку `main` (`git log --oneline main`), а не вычеканенную.
- **Перед деплоем смотреть `git status` на проде**: там бывают незакоммиченные патчи (`thinking`/`effort` в `_make_options`, 08.2026); `git pull` их уронит конфликтом или затрёт молча. Нужный патч вносится в код и пушится, а не остаётся dirty.
- **Смоук `import bot` перед рестартом** (07–08.2026, #14): ловит ImportError до того, как сервис упадёт. Рецепт токена — `rules-verification.md`.
- Xray на 443/8443 того же хоста — VPN владельца, не трогать (07.2026, #6: миграция на общий хост; поломка отрежет доступ владельца).

Репозиторий публичный
- **`DrSeedon/kesha-tg-bot` публичный; `artifacts/` не версионируется** (15.09.2026: в локальном `main` на проде накопилось 13 коммитов с 370 фото учеников клиента — один `git push` опубликовал бы их). Каталог в `.gitignore`, на проде `pre-push`-хук рубит пуш с путями `artifacts/` (кроме шести старых `artifacts/*.html`).
- **В репозиторий не класть chat id, IP и адреса серверов, имена людей, суммы, тексты переписки и пути к личным заметкам** (владелец, границы задач V-45/V-46/V-48, 17.10.2026): сырьё замеров держать вне репо, в `.orchestra/tasks/` — агрегаты. Примеры и логи с `thread/resume` содержат живую переписку.

Git
- **Перед коммитом в долгоживущей ветке — `git fetch && git diff --stat origin/main`, УДАЛЁННЫЕ строки прочитать глазами** (31.07–01.08.2026, поймано трижды за сессию: правила деплоя, adaptive thinking, фикс #14 = −109 строк). `git add <файл>` из ветки со старой базой записывает файл целиком в устаревшей версии; зелёные тесты откат не ловят — код и тесты откатываются синхронно.

Проверки
- **Полный сьют — на версиях ПРОДА** (04.09.2026; 23.09 обновлено до SDK 0.2.158, где в SDK вшит свой Claude Code, а Opus 5.5 требует ≥2.1.280): `uv run --isolated --no-project --exclude-newer 2030-01-01 --with-requirements requirements.txt --with mcp==1.28.1 --with claude-agent-sdk==0.2.158 --with pytest --with pytest-asyncio python -m pytest -q tests` (дольше минуты — `bg_create`). Версии сверять: `/opt/kesha-bot/.venv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"`. Без пинов сьют падает на `test_kesha_mcp_proxy` (новый `mcp` убрал `Server.list_tools`) — не регрессия задачи.
- **Под `.orchestra/tasks/<id>/` лежит запускаемый код, сьют его не покрывает** (06.09.2026, #19: смена `ChatRegistry(mcp_config=)` → `mcp_loader=` оставила боевой пробник с `TypeError` при 636 зелёных тестах). Меняя сигнатуру общего конструктора, грепать весь репозиторий, включая каталоги доказательств.
- **Секреты проверять по значениям из конфига, не по именам** (#19, 06.09.2026): подробности в `rules-verification.md`. В отчёты печатать только имена переменных.
- **Ветвление по подстроке ошибки — сначала более специфичные варианты** (07.2026: «session limit» ловился общим «session» и ретраился); лимиты и квота — ждать, не ретраить. Детали — `rules-incident-lessons.md`.
- Остальная методика (мутации, тест-окружение, ловушки веток) — `rules-verification.md`.

## Реестр: тема → файл → когда читать

| Тема | Файл | Когда читать |
|---|---|---|
| Прод: топология, деплой, рестарт, логи, вход Claude, туннель к ноутбуку | `.orchestra/kb/rules-prod-ops.md` | Деплой, рестарт, падение прода, 401, сеть |
| Копия оркестратора на VPS | `.orchestra/kb/rules-orchestrator-vps.md` | Переезд или починка сессии оркестратора, работа в чекауте на VPS |
| Ozon MCP (российский хост, регион, `ourPrice`) | `.orchestra/kb/rules-ozon.md` | Правка или сбой `ozon`, цены Ozon |
| Codex-рантайм, мост тулов, дыры T3 | `.orchestra/kb/rules-codex-runtime.md` | `codex_session.py`, `tool_bridge.py`, `file_access.py`, безопасность тулов |
| Методика проверок, грабли окружения и веток | `.orchestra/kb/rules-verification.md` | Тесты, мутации, сьют на VPS, странно красный тест |
| Прод-инциденты и замеры (латч лимита, TTL кеша, баны) | `.orchestra/kb/rules-incident-lessons.md` | Классификация ошибок провайдера, тишина бота, вопросы про SDK на подписке |
| Допуск по контексту и компакт | `.orchestra/kb/context-compaction.md` | Правка `_run_batch`, `compact.py`, резерва |
| Стоимость и токены ответа | `.orchestra/kb/cost-accounting.md` | Учёт расхода, `response_usage` |
| Типы сообщений Telegram, rich-сообщения | `.orchestra/kb/telegram-ingress.md` | Новые типы входящих |
| Что умеет бот для пользователя | `README.md` / `README.ru.md` | Любое описание возможностей: сверять с кодом, не с памятью |

Правило обслуживания: новое правило, нужное не всегда, идёт в тематический файл с датой и причиной (владелец, задача, инцидент); в ядро — только строка реестра или правило на всякий случай. Временное («пока», «до даты», «на время замера») не пишется в файлы, а ставится оркестратором как временное правило платформы. Не помещать в ядро отчёты, хронику и состояние на дату.
