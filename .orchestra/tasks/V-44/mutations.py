"""V-44 mutation matrix; run from the worktree root on a clean tree."""
import subprocess, sys
PYTEST = "/tmp/v43pytest.sh"
T = "tests/test_mid_turn_steer.py"
MUTANTS = [
    ("every mid-turn arrival goes to deferred (pre-V-44)", "chat_state.py",
     'if self.phase == ChatPhase.PROCESSING and callable(getattr(self.session, "steer", None)):',
     "if False:", f"{T}::test_message_mid_tool_is_sent_before_result_and_answered"),
    ("reader ends at our Result although a steer is owed", "claude_session.py",
     "if prompt_uuid is not None and not stream.pending:", "if prompt_uuid is not None:",
     f"{T}::test_steer_queued_by_the_cli_after_our_result_is_still_answered"),
    ("steer skips admission", "chat_state.py",
     "        reserve = await self.session.check_context_reserve(combined)\n        if reserve.get(\"should_compact\")",
     "        reserve = {\"ok\": True}\n        if reserve.get(\"should_compact\")",
     f"{T}::test_admission_refusal_keeps_the_message_for_the_next_batch"),
    ("failed steer write is dropped", "chat_state.py",
     "        if not await self.session.steer(combined):\n            await self._requeue(batch)\n            return",
     "        if not await self.session.steer(combined):\n            return",
     f"{T}::test_failed_steer_write_falls_back_to_the_queue"),
]
bad = 0
for name, path, old, new, test in MUTANTS:
    src = open(path).read()
    if src.count(old) != 1:
        print(f"BROKEN   {name}: anchor count {src.count(old)}"); bad += 1; continue
    open(path, "w").write(src.replace(old, new))
    try:
        run = subprocess.run([PYTEST, "-q", "--timeout", "20", "-p", "no:cacheprovider", test], capture_output=True, text=True)
    finally:
        subprocess.run(["git", "checkout", "--", path], check=True)
    tail = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else run.stderr[-200:]
    killed = run.returncode != 0 and "Timeout >" not in run.stdout
    print(f"{'KILLED  ' if killed else 'SURVIVED'} {name} -> {tail}"); bad += not killed
sys.exit(1 if bad else 0)
