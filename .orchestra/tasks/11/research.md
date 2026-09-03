# Incident research #11 — неожиданный рестарт Kesha

## Вывод

**CONFIRMED:** `kesha-bot-vps` не упал, не был убит OOM и не рестартовал по watchdog/`Restart=on-failure`. Его штатно остановил и сразу запустил `systemctl restart`, вызванный автоматическим `apt-daily-upgrade.timer` во время `unattended-upgrade` после обновления библиотек Kerberos.

Инициатор: `apt-daily-upgrade.timer` → `apt-daily-upgrade.service` → `/usr/bin/unattended-upgrade` → post-install service-restart step → `systemctl restart fwupd.service kesha-bot-vps.service ssh.service`. Формат этого step соответствует установленному `needrestart`, но per-process audit для прямой PID-привязки отсутствует.

## Временная шкала

VPS работает в `Europe/Berlin` (CEST, UTC+2), пользовательское время — `Asia/Krasnoyarsk` (UTC+7). Поэтому `06:27:23 CEST = 04:27:23 UTC = 11:27:23 KRAT`.

| Asia/Krasnoyarsk | VPS (CEST) | Событие |
|---|---|---|
| 11:25:57 | 06:25:57 | `apt-daily-upgrade.timer` сработал (`LastTriggerUSec`) |
| 11:25:58.248352 | 06:25:58.248352 | systemd начал `apt-daily-upgrade.service` |
| 11:26:53 | 06:26:53 | unattended-upgrade начал установку Kerberos-пакетов |
| 11:27:23.078076 | 06:27:23.078076 | systemd: `Stopping kesha-bot-vps.service...` |
| 11:27:23.091687 | 06:27:23.091687 | Python PID 785088: `Received SIGTERM signal` |
| 11:27:29.810467 | 06:27:29.810467 | systemd: `Deactivated successfully` |
| 11:27:29.813428 | 06:27:29.813428 | stop-job завершён, `JOB_RESULT=done` |
| 11:27:29.843765 | 06:27:29.843765 | systemd запустил новый процесс |
| 11:27:48.357325 | 06:27:48.357325 | новый PID 903396 загрузил MCP-серверы |
| 11:27:49.249688 | 06:27:49.249688 | бот установил команды и завершил штатный startup |

Stop/start на уровне systemd занял **6.766 с**; до завершения startup бота — **26.172 с**.

## Доказательства

### Автоматическое обновление и точная команда

`systemctl show apt-daily-upgrade.timer`:

```text
LastTriggerUSec=Fri 2026-07-24 06:25:57 CEST
RandomizedDelayUSec=1h
Unit=apt-daily-upgrade.service
```

Timer настроен как `OnCalendar=*-*-* 6:00` с `RandomizedDelaySec=60m`. Через 1.25 с журнал PID 1 зафиксировал:

```text
2026-07-24T06:25:58.248352+02:00 systemd[1]: Starting apt-daily-upgrade.service - Daily apt upgrade and clean activities...
```

`/var/log/apt/history.log`:

```text
Start-Date: 2026-07-24  06:26:55
Commandline: /usr/bin/unattended-upgrade
Upgrade: krb5-locales ... libgssapi-krb5-2 ... libkrb5support0 ... libkrb5-3 ... libk5crypto3 ...
End-Date: 2026-07-24  06:27:12
```

`/var/log/unattended-upgrades/unattended-upgrades-dpkg.log` содержит прямой положительный след инициатора:

```text
Log started: 2026-07-24  06:26:53
...
Restarting services...
 systemctl restart fwupd.service kesha-bot-vps.service ssh.service
...
Log ended: 2026-07-24  06:27:36
```

На сервере установлен `needrestart 3.6-7ubuntu4.5`, а приведённый post-install вывод соответствует его формату. Поэтому `needrestart` — высокоуверенная атрибуция промежуточного hook, но непосредственно доказанный инициатор на уровне unit/process chain — `apt-daily-upgrade.service`/`unattended-upgrade`, в чьём dpkg-журнале записана команда.

### Это штатный stop, не crash

`journalctl -u kesha-bot-vps.service -o short-iso-precise`:

```text
2026-07-24T06:27:23.078076+02:00 systemd[1]: Stopping kesha-bot-vps.service - Kesha Telegram Bot (Contabo)...
2026-07-24T06:27:23.091687+02:00 python3[785088]: Received SIGTERM signal
2026-07-24T06:27:29.810467+02:00 systemd[1]: kesha-bot-vps.service: Deactivated successfully.
2026-07-24T06:27:29.813428+02:00 systemd[1]: Stopped kesha-bot-vps.service - Kesha Telegram Bot (Contabo).
2026-07-24T06:27:29.843765+02:00 systemd[1]: Started kesha-bot-vps.service - Kesha Telegram Bot (Contabo).
```

Verbose journal для stop-job: `JOB_RESULT=done`, `CODE_FUNC=unit_log_success`. Числового failure exit status в историческом событии нет: процесс получил штатный `SIGTERM` (signal 15), а systemd классифицировал деактивацию как успешную. Текущий unit state показывает `Result=success`, `ExecMainCode=0`, `ExecMainStatus=0`.

Текущие `ExecMain*` и `NRestarts=0` относятся к новой активации PID 903396 и не являются историческим exit status PID 785088. Доказательство штатного завершения старой активации — именно исторические `SIGTERM` → `Deactivated successfully` → `JOB_RESULT=done`.

Unit имеет `Restart=on-failure`, `RestartSec=5s`, но историческая последовательность не соответствует автоматической restart policy: старый unit был успешно остановлен, а новый стартовал через 30 мс после завершения stop-job, без пятисекундной задержки. `WatchdogUSec=0`, watchdog отключён.

### Исключённые причины

- **OOM — REFUTED:** kernel journal в интервале 06:15–06:35 CEST не содержит OOM, killed-process, panic или segfault. В текущем boot есть старые OOM-события только от 2026-07-06, они не относятся к инциденту. `OOMPolicy=stop`.
- **Reboot VPS — REFUTED:** `journalctl --list-boots` содержит единственный текущий boot с 2026-06-30 09:58:07 CEST; `last -x` и `who -b` подтверждают тот же boot. Во время инцидента uptime не прерывался.
- **Ручной restart как причина — NOT SUPPORTED:** в `sudo`, auth и SSH journal нет успешного входа/команды в 06:00–06:29 CEST; первый последующий успешный SSH-вход был в 06:29:30, уже после рестарта. Положительное доказательство достаточной причины — timer сработал за секунду до запуска apt, а unattended-upgrades записал в границах транзакции 06:26:53–06:27:36 точную команду `systemctl restart ... kesha-bot-vps.service ...`.
- **Ограничение audit trail:** `auditd`/`ausearch` на VPS отсутствуют, `_TRANSPORT=audit` записей не имеет. Поэтому нельзя абсолютно исключить совпавший запрос из ранее открытой root-сессии или другого привилегированного локального процесса. Это не ослабляет положительный след unattended-upgrade как фактического инициатора наблюдаемого restart transaction, но не доказывает его криптографически уникальным вызывающим процессом.

## Текущий health

Снимок на `2026-07-24 06:34:54 CEST` (`11:34:54 KRAT`):

```text
ActiveState=active
SubState=running
MainPID=903396
Result=success
NRestarts=0
MemoryCurrent=193536000
ActiveEnterTimestamp=Fri 2026-07-24 06:27:29 CEST
```

После нового старта журнал не содержит текстовых совпадений `WARNING|ERROR|CRITICAL|Traceback|Exception`; проверка повторена на `2026-07-24 06:42:22 CEST`. Startup завершён: MCP-серверы загружены, inbox и reminder loop запущены, Telegram-команды установлены. Это подтверждает process-level health; end-to-end Telegram-запрос не отправлялся, поскольку диагностика была строго read-only.

## Гипотезы и фальсификаторы

1. **Crash/OOM/watchdog:** ожидались historical failure result, traceback/kernel OOM или watchdog timeout. Ничего из этого нет; гипотеза refuted.
2. **Ручной restart/reboot:** ожидались SSH/sudo/audit либо новый boot. Эти следы отсутствуют; ручной restart не поддержан доказательствами, reboot refuted. Без auditd нельзя абсолютно исключить совпавшую команду из уже открытой привилегированной сессии.
3. **Автообновление:** ожидались совпадающие timer trigger, apt transaction и явный service restart. Все три независимых следа совпали; гипотеза confirmed.

## Источники и уровень доказательности

Все источники — прямые измерения/первичные журналы VPS:

1. `systemctl status/show/cat kesha-bot-vps.service`.
2. `journalctl -u kesha-bot-vps.service` и verbose journal PID 1.
3. `systemctl show/list-timers/cat apt-daily-upgrade.timer`; `journalctl -u apt-daily-upgrade.service`.
4. `/var/log/apt/history.log`, `/var/log/dpkg.log`, `/var/log/unattended-upgrades/unattended-upgrades-dpkg.log`.
5. `journalctl -k`, `journalctl --list-boots`, `last -x`, `who -b`.
6. systemd journals `sshd`, `sudo`, `_TRANSPORT=audit`; наличие `auditd`/`ausearch`.
