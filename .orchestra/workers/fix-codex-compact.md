# fix-codex-compact

- On the VPS, the development venv has pytest but not ONNX Runtime; the prod
  venv has ONNX Runtime but not pytest. Run the full suite without installing
  into either environment via
  `PYTHONPATH=/home/kesha/projects/kesha-tg-bot/.venv/lib/python3.12/site-packages /opt/kesha-bot/.venv/bin/python -m pytest`.
- Kesha's private Codex rollouts live under
  `/opt/kesha-bot/storage/sessions/codex-home/sessions`; `~/.codex/sessions` is
  the wrong corpus for bot-thread production evidence.
- Never print a Codex app-server's full argv: MCP credentials are passed in
  `-c` arguments. Inspect PID/parent/timestamps or exact non-secret fields only.
