## Summary

Ну конечно, apt оставил больше улик, чем средний ручной рестарт 🕵️. Основной вывод выдерживает проверку: временные зоны и длительности рассчитаны правильно, а цепочка timer → unattended-upgrade → явная команда `systemctl restart` убедительно объясняет рестарт. Блокирующих проблем нет.

## Findings

### suggestion: Текущие поля systemd не доказывают статус старого процесса

[research.md:76](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/investigate-restart/docs/tasks/11/research.md:76)

`ExecMainCode=0`, `ExecMainStatus=0` относятся к уже запущенному PID 903396 и ничего не говорят о завершении PID 785088. Аналогично, `NRestarts=0` — состояние текущей активации, а не долговечная история unit. Вывод всё равно подтверждается исторической последовательностью `Stopping → SIGTERM → Deactivated successfully → Started` и отсутствием `RestartSec=5s`, но текущие поля не следует считать историческим доказательством.

### suggestion: Прямая улика не привязана в цитате к конкретной транзакции

[research.md:55](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/investigate-restart/docs/tasks/11/research.md:55)

Самая важная цитата — `systemctl restart ... kesha-bot-vps.service` — приведена без временной метки или границы сессии журнала. Поэтому из показанного фрагмента нельзя независимо установить, что строка относится именно к транзакции 06:26–06:27. Наличие `needrestart` и характерный формат вывода делают атрибуцию правдоподобной, но точное звено `needrestart → systemctl` в представленном доказательстве скорее уверенно выведено, чем непосредственно показано.

### suggestion: Ручной рестарт не исключён логически до конца

[research.md:84](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/investigate-restart/docs/tasks/11/research.md:84)

Отсутствие новых SSH-входов и `sudo`-записей исключает только эти наблюдаемые пути. Ранее открытая root-сессия уже упомянута, но также остаются привилегированные локальные процессы или совпавший запрос через systemd/D-Bus; без auditd уникального инициатора доказать нельзя. Положительный apt-след убедительно устанавливает, что unattended-upgrade инициировал рестарт, однако формулировки `REFUTED` и «однозначно» сильнее доступного доказательства эксклюзивности.

### question: Чем проверено отсутствие warning/error после старта?

[research.md:101](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/investigate-restart/docs/tasks/11/research.md:101)

Если использовался `journalctl -p warning`, проверка может пропускать прикладные ошибки: Python пишет уровень текстом через `StreamHandler`, а не syslog-приоритетом ([config.py:42](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/investigate-restart/config.py:42)). Если проверялись текстовые `WARNING`/`ERROR` либо файловый лог, утверждение обосновано. Остальная часть health-снимка корректно доказывает process-level startup, но не end-to-end работу Telegram.

## Verdict

**Поддержан с высокой уверенностью; blocking-находок нет.** `apt-daily-upgrade`/`unattended-upgrade` почти наверняка вызвал рестарт, а crash, OOM, watchdog и reboot убедительно исключены. Ослабить нужно только абсолютность атрибуции `needrestart` и исключения параллельного ручного запроса.

Виновник найден; просто в протоколе его отпечатки местами подписали как алиби. 🧾

## Round (2026-07-24T04:44:56Z)

## Summary

Ну вот, почти все улики наконец подписаны правильно 🔍. Round 2: три прежние находки исправлены полностью, одна оставила небольшой хвост; новых проблем нет.

- `STILL BROKEN` — текущие `ExecMain*` исправлены, но `NRestarts` остался историческим фальсификатором в итоговой гипотезе.
- `FIXED` — команда рестарта привязана к границам apt-транзакции, `needrestart` корректно назван высокоуверенной атрибуцией.
- `FIXED` — ручной рестарт теперь `NOT SUPPORTED`, ограничения аудита раскрыты.
- `FIXED` — health проверен текстово, датирован и ограничен process-level состоянием.

## Findings

### suggestion — STILL BROKEN: `NRestarts` снова используется как историческое доказательство

[research.md:111](/mnt/data/Projects/Python/orchestra/worktrees/mnt-data-projects-python-kesha-tg-bot/investigate-restart/docs/tasks/11/research.md:111)

Строка ожидает `NRestarts>0` при crash/watchdog, хотя выше отчёт правильно поясняет, что текущее значение относится к новой активации и не описывает PID 785088. Это внутреннее противоречие. Остальные исторические признаки — отсутствие failure/traceback/OOM/watchdog и явная последовательность штатного stop — достаточны, поэтому основной вывод не страдает.

## Verdict

**APPROVED — Round 2.** Блокирующих и новых находок нет; причинная атрибуция apt/unattended-upgrade теперь сформулирована соразмерно доказательствам.

Отчёт прошёл, только один `NRestarts` задержался в протоколе после окончания допроса. 🧾
