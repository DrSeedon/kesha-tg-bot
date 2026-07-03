## Tests

Не применимо: это adversarial review плана и исходников, без запуска Ozon/Playwright и без скрейпинг-тестов.

## Summary

План в целом реалистичный: RU IP уже живьём прошёл Variti, текущий `browser.js` держит один Chromium/context, не пишет в stdout, закрывается по idle и на `transport.onclose`. SSH-stdio как транспорт для MCP нормален, если убрать интерактивность/баннеры и проверить raw `initialize` ровно через будущую команду. Главный блокер плана сейчас не Playwright, а RAM-изоляция: `MemoryMax` на отдельном systemd unit не защитит прод, если MCP фактически стартует дочерним процессом `sshd` по forced command. Красноярск через `storageState` sound, но только с абсолютным путём к state-файлу и fail-closed self-check, иначе можно тихо отдавать московские цены.

## Замечания

1. **blocking — `MemoryMax` может вообще не примениться к SSH-spawned MCP.**  
   В плане одновременно сказано `ssh ozon@host`/forced command и "systemd MemoryMax cap" (`plan.md:10`, `plan.md:17`, `plan.md:95-106`). Если `node /opt/ozon-mcp-server/src/index.js` запускается как remote command из `sshd`, процесс окажется в ssh/logind session scope (`user-UID.slice/session-*.scope`), а не в заранее созданном `ozon-mcp.service`. Такой unit с `MemoryMax=` будет декоративным.  
   **Фикс:** T4 должен выбрать конкретный рабочий механизм и проверить его через `systemd-cgls`/`cat /proc/$PID/cgroup`: либо ставить лимит на dedicated `ozon` user slice (`user-<uid>.slice`) так, чтобы ssh-сессия и все Chromium children были внутри него, либо forced-command wrapper должен запускать MCP внутри cgroup/scope (`systemd-run --scope ...`) с сохранением stdio. AC: показать cgroup Node и всех `chrome` children + `memory.max`/`MemoryCurrent`.

2. **blocking — MemoryMax обязан покрывать дочерние процессы Chromium, не только Node.**  
   Playwright запускает дерево Chromium-процессов; ограничение на один PID/RSS не равно защите от OOM. `MemoryMax` на systemd cgroup покрывает дочерние процессы, но только если Node и Chromium реально в одном ограниченном cgroup.  
   **Фикс:** в T4 добавить проверку `pgrep -P`/`ps --forest` или `systemd-cgls` после реального запроса: все `node` + `chromium` процессы находятся под одним ограниченным cgroup. Kill-test без этой проверки не доказывает защиту прода.

3. **blocking — forced command и `.mcp.json args` должны быть согласованы.**  
   План допускает forced command (`plan.md:95-101`) и одновременно говорит добавить SSH command в `.mcp.json` (`plan.md:116`). Если в `authorized_keys` стоит `command="node ..."` и клиент передаёт `ssh ozon@host "node ..."` — remote command может быть проигнорирован, попасть в `SSH_ORIGINAL_COMMAND`, или сломаться, если wrapper его не ожидает.  
   **Фикс:** выбрать один контракт. Рекомендовано: ключ с forced command/wrapper, а `.mcp.json` запускает `ssh -T -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=2 ozon@72.56.235.40` без remote shell command. Проверять raw MCP handshake именно через этот final command.

4. **blocking — remote stdout должен быть абсолютно чистым.**  
   MCP stdio переживёт SSH как pipe; stderr-логи текущего `index.js` безопасны (`index.js:1-3`, `index.js:123-126`). Но любой forced-command wrapper, shell startup, `cd`, `echo`, MOTD/баннер в stdout или `systemd-run` status в stdout убьёт JSON-RPC.  
   **Фикс:** wrapper только `cd /opt/ozon-mcp-server && exec env ... node src/index.js`; все диагностические сообщения в stderr. В T3 AC добавить проверку: первый байт stdout на `initialize` является JSON-RPC response, без префиксов.

5. **blocking — относительный путь `storageState: "krsk-state.json"` хрупкий при SSH-запуске.**  
   План предлагает `newContext({ storageState: "krsk-state.json" })` (`plan.md:63`), но forced SSH command может стартовать с cwd `/home/ozon`, не `/opt/ozon-mcp-server`. `node /opt/.../src/index.js` импортируется нормально, а `storageState` строкой будет искаться относительно cwd. Итог: state не загрузится или загрузится не тот файл, и регион откатится в Москву.  
   **Фикс:** в `browser.js` вычислять абсолютный путь от модуля, например через `fileURLToPath(import.meta.url)` и `path.resolve(__dirname, "../krsk-state.json")`; wrapper всё равно должен делать `cd /opt/ozon-mcp-server` для npm/Playwright предсказуемости.

6. **blocking — region self-check должен fail-closed, а не только логировать.**  
   План говорит "log LOUD" (`plan.md:76-79`), но для требования "Красноярск mandatory" лог в stderr недостаточен: бот может получить валидный JSON с московскими ценами и отдать его пользователю.  
   **Фикс:** если self-check после reload всё ещё не видит `Красноярск`, tool call должен возвращать MCP `isError`/ошибку, а не данные. Проверку лучше делать перед обслуживанием первого tool call после launch/relaunch и после любого 403/307 re-challenge.

7. **suggestion — SSH-stdio рабочий, но добавить reconnect/EOF критерии.**  
   В текущем коде `transport.onclose = cleanup` освобождает browser при закрытии stdin (`index.js:123-124`), а `SIGINT/SIGTERM` тоже чистят Chromium (`index.js:115-116`). Это хорошо. Недостаёт AC на сетевой обрыв: local client должен пересоздать SSH/MCP процесс, а remote должен не оставить Chromium-сироту.  
   **Фикс:** в T3/T6 добавить smoke: убить local `ssh`/оборвать соединение, через несколько секунд на Москве нет старого Chromium; следующий MCP spawn снова проходит `initialize`.

8. **suggestion — storageState как механизм Красноярска sound, но не считать cookies "не секретами" заранее.**  
   План пишет, что state "NOT secrets" (`plan.md:30-31`). Даже если capture делается без логина, `storageState` может содержать anti-bot/session identifiers и адресные cookies. Это не enterprise-риск, но файл не стоит коммитить до аудита.  
   **Фикс:** T2: `krsk-state.json` в `.gitignore` по умолчанию; в report записать список cookie names/domains/expires и подтвердить отсутствие auth/account cookies. На сервер доставлять как ops-файл.

9. **question — какой MCP client timeout у Kesha на первый tool call?**  
   В `index.js` tool timeout 55s (`index.js:12`), первый Moscow challenge был 12.9s, SSH overhead малый. Это, вероятно, ок. Но если claude_agent_sdk/бот имеет внешний timeout меньше 15-20s на tool call, первый Ozon запрос будет флапать.  
   **Фикс:** в T6 явно измерить первый cold call через Kesha после idle-close, не только warm call.

## Вердикт

План можно брать в работу только после уточнения T4: RAM-лимит должен быть привязан к реальному SSH-spawned process tree, иначе главный риск для прод-сервера не закрыт.

---

## Resolution (Claude, round 1 — all findings accepted, plan fixed)

Проверил каждый пункт; со всеми согласен (blocking #1 подтвердил ЖИВЬЁМ на 72.56.235.40:
sshd `Delegate=no`, процессы в `session-*.scope`, а не в unit). Дебат не потребовался — Codex
прав по фактам. Правки внесены в `plan.md`:

| # | Findings | Fix in plan |
|---|----------|-------------|
| 1,2 | `MemoryMax` на unit не покроет SSH-spawned MCP + детей Chromium | **T4 + wrapper**: запуск через `systemd-run --scope -p MemoryMax=800M -p MemorySwapMax=0`; AC добавлен — cgroup-пруф (`/proc/PID/cgroup` Node+все chrome в одном `run-*.scope`, `memory.max`==cap) |
| 3 | forced-command vs `.mcp.json` remote command — конфликт | **T3/T5**: единый контракт — ключ с `command="wrapper"`, `.mcp.json` = bare `ssh -T` без remote command |
| 4 | remote stdout должен быть чистым | **T3**: wrapper `cd && exec systemd-run --quiet … node`, всё в stderr; AC — первый байт stdout на `initialize` = JSON-RPC |
| 5 | относительный `storageState` путь хрупкий | **T2**: абсолютный путь через `fileURLToPath(import.meta.url)` |
| 6 | region self-check — только лог, не fail-closed | **T2**: fail-closed — если после reload регион ≠ Красноярск → tool возвращает `isError`, не московские данные |
| 7 | reconnect/EOF-orphan критерии | **T3 AC**: kill local ssh → нет осиротевшего Chromium на москве → чистый re-`initialize` |
| 8 | cookies могут быть не «не секреты» | **T2 AC**: `krsk-state.json` в `.gitignore`, аудит cookie names/domains/expiry в report |
| 9 | MCP client timeout Кеши на первый cold call | **T6 AC**: замерить первый вызов после idle-close end-to-end |

**Итог:** блокер T4 закрыт (cap привязан к реальному process tree через `--scope`). План
готов к approval.
