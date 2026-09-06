# #36 — мутационная матрица

Каждая защита сломана в коде, прогон `tests/test_reload_cli.py`, тест обязан покраснеть.
Все мутации откатывались после прогона. Интерпретатор:
`/home/kesha/projects/kesha-tg-bot/.venv/bin/python -m pytest tests/test_reload_cli.py -q`.

| # | Мутация | Красный тест |
|---|---------|--------------|
| M1 | `_ensure_connected(preserve_session=True)` → `False` в `ClaudeSession.apply_mcp_servers` | `test_claude_reload_preserves_the_session_across_a_failed_connect` |
| M2 | Убрана проверка `if before and self.session_id != before` в обоих адаптерах | `test_claude_reload_raises_when_the_session_changes`, `test_codex_reload_raises_when_the_thread_changes` |
| M3 | `self.mcp_servers = servers` перенесено ПОСЛЕ коннекта (оба адаптера) | `test_claude_reload_respawns_with_the_new_servers_and_resumes`, `test_codex_reload_swaps_servers_before_the_app_server_starts` |
| M4 | Снят фазовый гейт `if self.phase is not ChatPhase.IDLE` | `test_reload_refused_while_a_turn_is_active[processing/stopping/compacting]` |
| M5 | `load_mcp_servers` обёрнут в `lru_cache` (перестал перечитывать диск) | `test_reload_sees_a_server_added_after_startup` |
| M6 | Убран `logger.warning` про нечитаемый `.mcp.json` | `test_unreadable_mcp_json_is_reported` |
| M7 | `kesha-bot-vps` → `kesha-bot` в `h_restart` | `test_restart_targets_the_unit_that_exists` |
| M8 | Снят `finally: _finish_reload()` (фаза не отпускается на ошибке) | `test_reload_failure_does_not_latch_the_chat` |
| M9 | Убран `self.phase = ChatPhase.PROCESSING` (чат не держится на время рестарта) | `test_a_message_sent_during_the_reload_is_answered_afterwards` |

Полный сьют на прод-версиях (`mcp==1.28.1`, `claude-agent-sdk==0.2.128`):
**636 passed, 3 skipped, 64s**. Эталон до правок — 618 passed, 3 skipped; +18 = ровно новые тесты.

## Что осталось непроверенным

- Боевого прогона `/reload` на проде не было: воркер не деплоит. Проверены контракт
  (`options.resume`, содержимое внешнего `--mcp-config`) и фазовая механика, но не то,
  что реальный `claude` CLI поднимает новый сервер — это проверяется первым `/reload` на Contabo.
- Codex-ветка проверена на заглушках `_connect`/`_teardown_process`: реальный `app-server`
  не запускался (квота Codex выжжена).
- Отложенный `/compact`, запрошенный во время релоада, обрабатывается общей машинерией хода —
  как и при `switch_runtime`. Отдельного теста не добавлял: поведение предсуществующее.
