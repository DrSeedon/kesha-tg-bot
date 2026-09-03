# #6 — реальные окна квоты вместо голого «жду сброса»

## Что было

```
⏳ Достигнут лимит подписки claude. Жду сброса — напиши позже.
```

Пользователь не знает, какое окно выжжено, сколько осталось и опережает ли расход темп окна.

## Что стало (живой прогон против прода, 11.08.2026)

```
5h: 12% (6%) 4h 42m · темп +18m
7d: 17% (3%) 6d 18h 32m · темп +23h 6m
```

Чтение: `утилизация% (сколько % окна прошло) осталось-до-сброса · темп`.

## Источники данных

| Рантайм | Откуда | Формат `resets_at` |
|---|---|---|
| Claude | `GET https://api.anthropic.com/api/oauth/usage`, Bearer из `~/.claude/.credentials.json` → `claudeAiOauth.accessToken`, `anthropic-beta: oauth-2025-04-20` | ISO-8601 со смещением |
| Codex | уже лежит в `codex_session.rate_limit` (`primary`/`secondary`) | unix-секунды |

Обе формы нормализуются в один вид `{label, utilization, resets_at: datetime, window_minutes}`
и рендерятся ОДНОЙ функцией — второго способа показать окно в проекте нет.

Живая проверка эндпоинта (`quota.fetch_claude_usage()` под юзером `kesha`):
```
{'five_hour': {'utilization': 12.0, 'resets_at': '2026-08-11T17:10:00.642929+00:00', ...},
 'seven_day': {'utilization': 17.0, 'resets_at': '2026-08-18T06:59:59.642953+00:00', ...}}
```

## Формулы — 1:1 с дашбордом Orchestra (`app/static/js/usage.js:27-59`)

- countdown: `h/m` из остатка; при `h >= 24` → `Nd Nh Nm`;
- «прошло окна»: `elapsed = window - remaining`, `round(elapsed/window*100)`;
- темп: `delta = util - elapsed/window*100`; `delta <= 5` → «темп ok», иначе
  `cooldown_min = round(delta * window_min / 100)`, формат `Nm` / `Nh Nm` / `Nd Nh Nm`.

Одно отклонение от JS, сознательное: `remaining` зажимается в `[0, window]` до расчёта.
В JS протухший `resets_at` даёт отрицательный `elapsed` и бессмысленный темп; на реальных
данных поведение совпадает, на мусорных — не врёт.

**Сверка примера из постановки.** В задании образец строки был `7d: 15% (3%) 6d 19h 8m · темп +20h 21m`.
На этих же входных данных формула даёт `+20h 20m` (delta 12.103% × 10080 / 100 = 1219.99 → 1220 мин).
Образец в постановке — снимок с дашборда на другую секунду, а не другая формула; в тест записан
результат ПЕРЕСЧЁТА, не образец.

## Куда вкручено

| Точка | Файл | Как |
|---|---|---|
| терминальное сообщение о лимите в стриме | `response_stream.py:_handle_usage_limit` | блок отдельными строками под текстом |
| отказ до хода (`usage_limit`) | `chat_state.py:_limit_fmt` (стал async) | тот же блок |
| `/status` | `handlers.py:h_status` | блок в хвост |
| `/runtime` | `handlers.py:_runtime_quota_line` | блок ВМЕСТО однострочной `runtime_quota` |

Нет данных → плейсхолдер `{quota}` пустой, текст сообщения ровно тот же, что был.
Строка `runtime_quota` осиротела моей же правкой и удалена; `runtime_quota_unknown` осталась
как фолбэк `/runtime`.

## Гарантии

- **Ход не роняем.** `quota_block()` ловит всё и возвращает `""`; сообщение о лимите объясняет
  сбой — потерять объяснение из-за похода за цифрами хуже, чем потерять цифры.
- **Кэш 60 с под мьютексом**, включая отрицательный результат: протухший токен иначе стоил бы
  одного мёртвого round-trip на КАЖДОЕ сообщение, а чаты спрашивают одновременно.
- **401 не чиним** — refresh делает CLI сам; отдаём `None` и показываем прежний текст.
- **Тесты не ходят в сеть**: `tests/conftest.py` autouse-фикстурой глушит `fetch_claude_usage`
  для всего сьюта, HTTP-тесты подсовывают фейковый модуль `aiohttp` через `sys.modules`.

## Codex-ревью (`codex-review-impl.md`)

Раунд 1 — REQUEST CHANGES, 1 blocking + 2 suggestion. Вердикт засчитан: ревьюер привёл команду
прогона и её вывод (`.venv/bin/python -m pytest tests/test_quota.py tests/test_runtime_limits.py -q`
→ `45 passed`).

**blocking — `response_stream.py`: вычисления ДО входа в защищённый `quota_block()`.**
Принято частично и починено. Проверка по коду: `_lang_of` разыменовывал `message.from_user`
ровно так же, как это делал `_t_cfg` в main, — то есть риск не новый, вопреки формулировке
находки. Но путь существует ради объяснения сбоя, а страховка стоит четырёх строк:
- `config.lang_of` сделан тотальным (`getattr(msg, "from_user", None)`) — чинит и `t()` заодно,
  один владелец правила вместо двух try/except на местах вызова;
- `_get_session(cid)` в `_handle_usage_limit` обёрнут: падение → `session=None`, блок пустой,
  сообщение уходит.
Добавлены два теста, оба мутационно проверены (см. таблицу, №10-11).

**suggestion — холодный кэш задерживает уведомление до 10 с.** Отклонено. 10 с — потолок таймаута,
типичный живой ответ эндпоинта — доли секунды. Предложенная альтернатива (отдать старый текст
сразу, дообогатить асинхронно) удваивает машинерию доставки на самом хрупком пути ради худшего
случая, который случается при мёртвой сети — там же, где и всё остальное уже сломано.

**suggestion — `/runtime` может висеть 30 с + 10 с = 40 с.** Отклонено: сценарий недостижим.
`read_quota`/`quota_summary` определены ТОЛЬКО в `codex_session.py` (`grep -n "def read_quota"` →
одна строка, 762). На Codex `quota_block` ходит в `session.rate_limit` и в сеть не идёт вовсе;
на Claude `read_quota` отсутствует, и 30-секундная ветка не выполняется. Складывать нечего.

Раунд 2 — **APPROVED**, новых находок нет. Вердикт засчитан: приведена дословная строка
`PACE_TOLERANCE_PCT = 5.0`, которой не было в запросе (`quota.py:26`, проверено грепом).
Оба отклонения приняты ревьюером: про 10 с — «reasonable minimal-design tradeoff», про 40 с —
«Codex reads `rate_limit` locally, while Claude lacks `read_quota`».

## Прогон

```
.venv/bin/python -m pytest tests/ -q
547 passed, 1 skipped in 25.28s
```
Смоук: `TELEGRAM_BOT_TOKEN=<фейк> python -c "import bot"` → `import bot OK`
(без токена падает на `validate_token` — окружение, не код).

Локального `.venv` в репозитории не было — создан `uv`-ом из `requirements.txt` + pytest;
`--exclude-newer 2030-01-01` обязателен, иначе `claude-agent-sdk>=0.2.128` не резолвится
(глобальный `exclude-newer = "7 days"` в `~/.config/uv/uv.toml`).

## Мутационная проверка

Протокол: `cp F F.bak` → мутация → прогон → `mv F.bak F` одной командой, свой `cp` на каждую мутацию.

| # | Мутация | Красные тесты |
|---|---|---|
| 1 | `delta * window_min / 100` → `/ 200` (темп) | 3 |
| 2 | `delta <= PACE_TOLERANCE_PCT` → `<= PACE_TOLERANCE_PCT * 100` (порог «темп ok») | 3 |
| 3 | `remaining_sec / 60` → `/ 30` (прошло окна) | 4 |
| 4 | `if hours >= 24` → `>= 240` (формат >24ч) | 3 |
| 5 | `if resp.status != 200` → `!= 999` | `..._successful_fetch...` |
| 6 | `if resp.status != 200` → `if False` | `..._expired_token...`, `..._failure_is_cached_too` |
| 7 | `"quota": f"\n\n{block}" if block else ""` → `""` (chat_state) | `..._reserve_limit_notice_shows_the_windows` |
| 8 | то же в `response_stream` | `..._stream_limit_notice_shows_the_windows` |
| 9 | `quota=f"\n\n{block}" if block else ""` → безусловное `f"\n\n{block}"` | `..._no_quota_data_leaves_the_old_message_untouched` |
| 10 | снят `try/except` вокруг `_get_session(cid)` в `_handle_usage_limit` | `..._notice_survives_a_registry_that_dies_at_the_limit` |
| 11 | `lang_of` возвращён к `msg.from_user.language_code` без `getattr` | `..._message_without_a_sender_still_renders_a_notice` |

Мутации 5 и 6 разделены намеренно: по одной 401-ветка не краснеет — проверка «статус ровно 200»
имеет два признака, и первая мутация меняет только то, какая ветка срабатывает на 200.
Мутация 9 добавлена после того, как первая версия теста «нет данных → текст не изменился»
оказалась пустой: она проверяла `not text.rstrip().endswith("\n")`, что истинно ВСЕГДА после
`rstrip()`. Переписана на сравнение финального пузыря с его же `strip()`.
