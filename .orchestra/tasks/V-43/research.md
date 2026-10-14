# V-43 — исследование: почему фоновые агенты ломают сессию Кеши

Окружение замеров: SDK `claude-agent-sdk==0.2.158` (как на проде, `/opt/kesha-bot/.venv`), вшитый
CLI 2.1.280, 14.10.2026. Скрипты и сырой вывод лежат в `spikes/`.

## 1. Причина таймаутов пробы: никто не читает поток между ходами

Посылка задачи («CLI был занят собственным ходом») опровергнута дважды.

**Транскрипт инцидента** (`d0b4afb0-….jsonl`, время UTC = KRSK−7). Последний ход по
notification («Готово 7 из 9») закончился в 10:29:40. Следующий `queue-operation enqueue` случился
только в 10:30:18 (это уже notification «2 background agents didn't finish», порождённая нашим
reconnect). Сообщение пользователя пришло в 10:29:47, пробы падали в 10:29:57 и 10:30:07. Значит,
CLI в момент обеих проб **простаивал**. Первая проба отвалилась ещё раньше: `get_context_usage`
в `_finish_processing` (17:28:10 → таймаут SDK 60 с в 17:29:10), через 3 с после Result.

**Механизм** (исходник SDK `_internal/query.py`). Читатель SDK `_read_messages` в одном цикле
маршрутизирует `control_response` и кладёт обычные кадры в `anyio` memory stream с
`max_buffer_size=100` через блокирующий `await self._message_send.send(message)`. Бот читает этот
поток только внутри `send_message` и выходит из цикла на Result (`claude_session.py:1043`). После
этого кадры ходов по notification, `task_progress` ещё работающих агентов и stream-события
(`include_partial_messages=True`) копятся в буфере. На 101-м кадре цикл встаёт на `send`, и
`control_response` больше никто не разбирает. Любой control-запрос (наша проба, а также
`can_use_tool` и вызовы in-process MCP от CLI) висит до таймаута.

**Замер** (`spikes/backpressure.py`: настоящий `Query` из SDK 0.2.158 поверх фейкового транспорта,
CLI-сторона отвечает на control_request сразу после N кадров, вывод в `spikes/backpressure.out`):

```
backlog=  50 drained=False: answered
backlog=  99 drained=False: answered
backlog= 100 drained=False: answered
backlog= 101 drained=False: TIMEOUT (TimeoutError)
backlog= 500 drained=False: TIMEOUT (TimeoutError)
backlog= 500 drained=True : answered
```

Граница проходит ровно по 100 кадрам. С потребителем, который читает поток, проба отвечает при
любом бэклоге. Сколько кадров было в инциденте, я не мерил: кадры потока в журнал не пишутся. Три
хода по notification со стримингом плюс `task_progress` от четырёх живых агентов за 100 с дают
больше сотни по порядку величины, но это оценка, а не замер.

## 2. Как CLI ведёт себя при постоянно читаемом потоке (`spikes/cli_behavior.py`, haiku)

Сырые события лежат в `spikes/cli_behavior.stdout`.

| Вопрос | Наблюдение |
|---|---|
| Q1. Отвечает ли `get_context_usage`, когда CLI занят? | Да. Простой с живым фоновым агентом: 0.50 с. Во время хода по notification: 0.01 с. Посреди 15-секундного foreground Bash: 0.02 с. |
| Q2. Эхо нашего промпта (`--replay-user-messages`) | `UserMessage` с **нашим** `uuid` (передан в dict сообщения) приходит, когда CLI **забирает** промпт в ход, а не когда мы его отправили. B отправлен в 16.65, эхо пришло в 20.06, уже после Result хода notification. E отправлен в 42.80 посреди тула, эхо пришло в 53.84 внутри того же хода. |
| Q3. `ResultMessage.origin` | У хода по notification `{"kind":"task-notification"}`. У наших ходов `None`. У нашего хода D, в который влились notification и промпт E, тоже `None`. |
| Q4. Промпт во время чужого хода | Отправленный во время хода notification (B) встаёт в очередь CLI и получает **свой** ход и свой Result. Отправленный посреди тула (E) вливается в текущий ход D: один Result («OK-D\n\nBANANA») отвечает на оба. |
| Прочее | Завершение фоновой задачи может влиться в наш ход без собственного Result (notification bash `bbb1l3g37` ушла в ход D). Новый тип кадра `command_lifecycle` SDK не разбирает (`parse_message` → None). |

Выводы для дизайна:
- Если поток дренируется, admission-проба работает при любой занятости CLI. Reconnect по её
  таймауту при живом читателе ничего не лечит и только убивает фоновых агентов.
- `origin` у Result не годится, чтобы определить, отвечает ли ход на наш вопрос: он описывает
  только **первый** промпт хода (в инциденте 10:30:22 notification и наш вопрос ушли в один ход).
  Надёжный признак — эхо с нашим `uuid`. Ход отвечает нам, если между предыдущим Result и его
  Result пришло эхо хотя бы одного нашего промпта.
- Ходы и Result не соответствуют промптам один к одному, ни в одну сторону. Счётчик
  `_expected_results` (+1 на `query()`, −1 на Result) по устройству неверен.

## 3. Как это сделано в Orchestra (`/home/kesha/orchestra`, только чтение)

`app/runtime_registry.py:334`: у Claude `event_stream="persistent"`.
`app/session.py:2101` `_activate_backend_tasks` поднимает один `_persistent_event_loop` на всё время
жизни бэкенда. Он читает `backend.events()` (= `client.receive_messages()`) непрерывно и
обрабатывает **каждое** событие одинаково, независимо от того, кто начал ход. Граница хода —
`turn_end`. Сообщение во время RUNNING уходит прямо в CLI (`session.py` `send` → `backend.send`, то
есть `client.query`), очередью занимается сам CLI. Вопрос «чей это Result» там не нужен: ответы
идут в общий лог, а не в конкретный ответ на конкретный вопрос.

## 4. Попутная находка

`ClaudeSession.inject()` вызывается только из тестов (`tests/test_claude_session_limit.py:273,358`).
В проде этот путь мёртв с #14.

## 5. `/stop` при живых фоновых агентах (`spikes/stop_spike.py`, haiku, CLI 2.1.280)

Сырые события лежат в `spikes/stop_spike.stdout`. Сценарий: ход A запускает фонового
агента (`local_agent` `a7cb…`), агент сам уводит команду в фоновый bash (`local_bash` `b8o0…`).
Затем идёт наш ход F, и посреди него вызывается `client.interrupt()` (так `/stop` делает
`ChatState.request_stop`).

```
14.27 interrupt
14.31 task_notification a7cb… (local_agent) status=stopped
14.31 user "[Request interrupted by user]"  (главный агент и субагент)
14.34 task_notification b8o0… (local_bash)  status=stopped
14.35 result error_during_execution, terminal_reason=aborted_streaming, is_error=true
```

Дальше за 75 с не пришло ни одного кадра. **Interrupt в режиме SDK останавливает не только
текущий ход, но и все фоновые задачи CLI: и агентов, и их фоновые shell-команды.** Отдельного
interrupt «только для хода» в control-протоколе SDK 0.2.158 нет. `stop_task(task_id)` умеет
только останавливать задачу, но не сохранять её.

Следствие для реализации. `/stop` остаётся нативным interrupt, другого способа остановить ход
нет. Если фоновые агенты живы, ответ на `/stop` называет, сколько задач будет остановлено вместе
с ходом, чтобы это не было сюрпризом. Терминальный Result прерванного хода
(`error_during_execution`, `aborted_streaming`, `result=None`) ошибкой рантайма не считается.
