## Summary

План в целом рабочий по направлению: сохраняет `/opt/cog-second-brain`, переносит `storage/`, копирует Claude auth/MCP/Gmail OAuth, убирает proxy и держит Timeweb как rollback. Главная проблема: финальный cutover синхронизирует только `storage/`, но не финальные Claude session histories (`~/.claude/projects/-opt-cog-second-brain/*.jsonl`). При живом старом боте это прямой риск потери последних turns/контекста.

## Findings

blocking: B2 снимает `~/.claude/projects/-opt-cog-second-brain/` пока старый бот еще работает, а F3 в downtime копирует только `/opt/kesha-bot/storage` -> все Claude `.jsonl` turns, появившиеся между B2 и F1, останутся только на Timeweb, при этом `messages.db` и `storage/sessions/*` на Contabo будут уже финальные. Fix: после F1 stop и перед F4 стартом Contabo сделать финальный sync как минимум `/home/kesha/.claude/projects/-opt-cog-second-brain/` (лучше вместе с релевантным mutable Claude state), затем проверить, что UUID из `storage/sessions/<chat_id>` имеет соответствующий `.jsonl` на Contabo.

blocking: rollback описан как "max loss = few messages processed by Contabo", но контекст задачи требует "ничего не потерять" -> при провале после live smoke любые сообщения, RAG-записи и Claude `.jsonl` turns, обработанные Contabo, не попадут обратно на Timeweb. Fix: в rollback добавить обязательный pre-rollback snapshot Contabo (`storage/` + `~/.claude/projects/-opt-cog-second-brain/`) и явное решение: либо переносить этот delta обратно на Timeweb перед стартом старого бота, либо проводить smoke только disposable-командами с заранее принятой потерей.

blocking: single-bot-polls-token invariant проверяется только через `systemctl stop kesha-bot-vps`; если на Timeweb есть второй `bot.py`/старый автозапуск/ручной процесс, F4 запустит второго poller на том же токене. Fix: после F1 и до F4 добавить process-level gate: `systemctl is-active --quiet kesha-bot-vps && exit 1 || true`, `pgrep -af 'bot.py|kesha-bot'` должен быть пустым для Timeweb, и только после этого стартовать Contabo. Команду `systemctl stop ... && sleep 2 && systemctl is-active ...` лучше не использовать как success-check, потому что `is-active` для остановленного сервиса возвращает non-zero.

suggestion: F2 checkpoint печатает результат `PRAGMA wal_checkpoint(TRUNCATE)`, но не валит миграцию при `busy > 0` и не делает `quick_check` -> можно продолжить с неочевидным WAL/SQLite состоянием. Fix: в Python-шаге проверять `r[0] == 0`, делать `PRAGMA quick_check`, abort при любом отличии от `ok`, затем явно проверять размеры `*.db-wal` и наличие всех трех DB.

suggestion: tar-over-ssh шаги не защищены от частичных архивов: если remote `tar`/ssh оборвется, локально останется `.tgz`, который следующий шаг может попытаться отправить/распаковать. Fix: после каждого tar делать `tar tzf`, `sha256sum`, `ls -lh`, а на Contabo сверять checksum до extract; для финального `storage.tgz` это особенно важно.

suggestion: A1 глотает ошибки `groupadd/useradd` через `|| true` и потом полагается на `kesha:kesha`. Если uid/gid 1001 занят чужим пользователем/группой, ownership станет не тем или `chown kesha:kesha` сломается позже. Fix: добавить preflight `getent passwd 1001`, `getent group 1001`, `id kesha`; abort, если 1001 занят не `kesha`, и только потом создавать пользователя.

suggestion: D1 чистит proxy только в `/opt/kesha-bot/.env`, но proxy env мог быть скопирован в `.claude.json`, `.claude/settings.json`, `.mcp.json` или MCP server configs. Fix: после D1 добавить target grep по `/opt/kesha-bot`, `/opt/cog-second-brain/.mcp.json`, `/home/kesha/.claude.json`, `/home/kesha/.claude/settings.json`, `/home/kesha/.claude/mcp-servers` на `HTTPS?_PROXY|TG_PROXY|NO_PROXY|127.0.0.1:10809` и удалить только реальные proxy-настройки.

suggestion: D3 добавляет `AllowTcpForwarding yes`, но не проверяет effective sshd config для пользователя `tunnel`; `Match`-блоки или другие ограничения могут все равно запретить reverse forwarding. Fix: добавить проверку `sshd -T -C user=tunnel,host=localhost,addr=127.0.0.1 | grep -E 'allowtcpforwarding|gatewayports|permitlisten'` и реальный тест с laptop/key: `ssh -N -R 127.0.0.1:2222:localhost:22 -o ExitOnForwardFailure=yes tunnel@158.220.127.161`.

suggestion: F5 сначала рестартит laptop service, а только потом принимает новый host key. Если unit не использует `StrictHostKeyChecking=accept-new`, первый restart может упасть и больше не подняться. Fix: сначала добавить host key (`ssh-keyscan` или одноразовый `ssh -o StrictHostKeyChecking=accept-new ...`), затем `systemctl restart ssh-tunnel-vps`, затем на Contabo проверить `ss -ltn | grep ':2222'`.

nit: rollback text говорит, что Timeweb DB "never modified -- we only READ them", но F2 `wal_checkpoint(TRUNCATE)` физически меняет DB/WAL файлы на Timeweb. Это нормально для rollback, потому что логические данные сохраняются, но формулировку лучше заменить на "не удаляем и не меняем бизнес-данные".

## Verdict

требует доработки

## Round 2 — re-review

### Previous blocking

- blocker#1 — FIXED. F3 теперь после F1 stop синхронизирует и `/opt/kesha-bot/storage`, и `/home/kesha/.claude/projects/-opt-cog-second-brain`, проверяет tar/checksum и валит миграцию при пустом/отсутствующем `.jsonl` для проверяемых session UUID.
- blocker#2 — FIXED. Rollback Step 0 сначала останавливает Contabo, снимает snapshot `storage/` + `.claude/projects/-opt-cog-second-brain/` и требует явного решения merge/discard до рестарта Timeweb.
- blocker#3 — NEW BUG. F1 cutover-gate исправлен, но rollback-gate не fatal: `pgrep -af bot\.py || echo "no contabo poller"` только печатает найденный Contabo poller и всё равно позволяет стартовать Timeweb. Нужно зеркалировать F1: после stop/disable проверить `is-active` по слову и `pgrep`, а при найденном процессе делать `exit 1`.

Заявленные suggestions тоже подтверждены в плане: F2 `busy==0` + `quick_check`, tar `tzf` + `sha256sum`, A1 uid/gid preflight, D1.5 proxy grep, D3 `sshd -T` + реальный reverse-forward test, F5 `ssh-keyscan` до restart.

### New findings

blocking: rollback всё ещё может привести к двум poller-ам на один TG token, если на Contabo останется ручной/stray `bot.py` после `systemctl stop/disable`; текущий Step 1 это не блокирует.

### Verdict

требует доработки

## Round 3 — re-review

Rollback Step 1 теперь зеркалит F1: `stop+disable`, проверка `is-active` по слову, `pgrep -af 'bot\.py'` и `exit 1` при найденном poller-е. Так как `ssh` вернёт remote rc, Step 2 со стартом Timeweb выполняется только после успешного quiesce Contabo.

Риск двух poller-ов закрыт. Оставшихся BLOCKING issues по rollback-секции не вижу с учётом личного 2-user bot и допустимого небольшого downtime.

### Verdict

APPROVED
