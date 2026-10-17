"""V-47 mutation matrix; run from the worktree root."""
import subprocess, sys
PY = "/home/kesha/projects/kesha-tg-bot/.venv/bin/python"
T = "tests/test_background_progress.py"
MUTANTS = [
    ("every notification turn is a new message (pre-V-47)", "response_stream.py",
     'return callable(count) and count() > 0', 'return False',
     f"{T}::test_notifications_while_agents_run_update_one_message_then_one_final"),
    ("progress message never reused", "response_stream.py",
     "        mid = _progress_messages.get(cid)\n", "        mid = None\n",
     f"{T}::test_notifications_while_agents_run_update_one_message_then_one_final"),
    ("background agent's tools reach the bubble (pre-V-47)", "claude_session.py",
     'if from_subagent and f"tool:{parent}" not in open_work:',
     "if from_subagent and not stream.turn_open:",
     f"{T}::test_background_agents_tools_do_not_tear_the_text"),
    ("answer merged into a progress turn is only an edit", "response_stream.py",
     'if progress_turn and not chunk.get("ours", False):', "if progress_turn:",
     f"{T}::test_question_merged_into_a_progress_turn_is_answered_as_a_new_message"),
]
bad = 0
for name, path, old, new, test in MUTANTS:
    src = open(path).read()
    if src.count(old) != 1:
        print(f"BROKEN   {name}: anchor count {src.count(old)}"); bad += 1; continue
    open(path, "w").write(src.replace(old, new))
    try:
        run = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
                             capture_output=True, text=True, timeout=120)
    finally:
        open(path, "w").write(src)
    tail = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else run.stderr[-200:]
    killed = run.returncode != 0
    print(f"{'KILLED  ' if killed else 'SURVIVED'} {name} -> {tail}"); bad += not killed
sys.exit(1 if bad else 0)
