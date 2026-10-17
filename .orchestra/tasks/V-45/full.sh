#!/bin/bash
cd /home/kesha/orchestra/worktrees/home-kesha-projects-kesha-tg-bot/feat-text-memory
uv run --isolated --no-project --exclude-newer 2030-01-01 --with-requirements requirements.txt --with mcp==1.28.1 --with claude-agent-sdk==0.2.158 --with pytest --with pytest-asyncio python -m pytest -q tests > .orchestra/tasks/V-45/full-suite.log 2>&1
echo "EXIT=$?" >> .orchestra/tasks/V-45/full-suite.log
