"""V-43 mutation matrix: each protection removed must turn a committed test red.

Run from the worktree root on a clean tree: python3 .orchestra/tasks/V-43/mutations.py
Each mutant is applied in place, its test runs, then `git checkout` restores the file.
"""
import subprocess
import sys

PYTEST = "/tmp/v43pytest.sh"
T = "tests/test_background_turns.py"

MUTANTS = [
    ("pump reads only while a consumer is attached (pre-V-43)", "claude_session.py",
     "                self._track_agents(msg)\n",
     "                while not self._consumers:\n                    await asyncio.sleep(0.05)\n                self._track_agents(msg)\n",
     f"{T}::test_frames_between_turns_never_starve_the_control_channel"),
    ("every Result answers the question", "claude_session.py",
     "ours = prompt_uuid is not None and prompt_uuid in stream.turn_uids",
     "ours = prompt_uuid is not None",
     f"{T}::test_foreign_empty_result_before_our_turn_is_not_the_answer"),
    ("prompt merged into a notification turn is not ours", "claude_session.py",
     "                stream.pending[msg.uuid] = True\n                stream.turn_uids.add(msg.uuid)\n",
     "                stream.pending[msg.uuid] = True\n",
     f"{T}::test_prompt_merged_into_a_notification_turn_counts_as_ours"),
    ("probe timeout reconnects although the stream is alive", "claude_session.py",
     "            if self._pump_alive():\n                # The stream is drained",
     "            if False:\n                # The stream is drained",
     f"{T}::test_admission_probe_answers_while_the_cli_works_and_never_reconnects"),
    ("CLI-started turns are not delivered", "chat_state.py",
     "            if not self._session_has_background_turn():\n                return\n            self.phase = ChatPhase.PROCESSING",
     "            return\n            self.phase = ChatPhase.PROCESSING",
     f"{T}::test_notification_turns_reach_the_chat_without_killing_the_cli"),
    ("a real stall no longer stops the CLI", "response_stream.py",
     "        if abort is not None:\n            await abort()",
     "        if False:\n            await abort()",
     f"{T}::test_real_stall_inside_a_cli_turn_still_stops_the_cli"),
    ("an untaken prompt is waited for forever", "claude_session.py",
     "                if pickup_deadline is None:\n                    pickup_deadline = loop.time() + PROMPT_PICKUP_TIMEOUT_S",
     "                pass",
     f"{T}::test_prompt_never_taken_is_a_clear_error_not_an_endless_wait"),
    ("compact ignores running background agents", "chat_state.py",
     "            postponed = 0 if automatic else self.background_agents_open()",
     "            postponed = 0",
     f"{T}::test_compact_waits_for_background_agents"),
    ("between-turn silence is timed like a stall", "claude_session.py",
     "            if prompt_uuid is None and not stream.frames and not stream.turn_open:\n                return\n",
     "",
     f"{T}::test_quiet_gap_between_turns_is_not_a_stall"),
]

failures = 0
for name, path, old, new, test in MUTANTS:
    src = open(path).read()
    if src.count(old) != 1:
        print(f"SKIP-BROKEN  {name}: anchor not unique ({src.count(old)})")
        failures += 1
        continue
    open(path, "w").write(src.replace(old, new))
    try:
        run = subprocess.run(
            [PYTEST, "-q", "--timeout", "20", "-p", "no:cacheprovider", test],
            capture_output=True, text=True,
        )
    finally:
        subprocess.run(["git", "checkout", "--", path], check=True)
    tail = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else run.stderr[-200:]
    killed = run.returncode != 0 and "Timeout >" not in run.stdout
    print(f"{'KILLED ' if killed else 'SURVIVED'} {name} -> {tail}")
    failures += not killed
sys.exit(1 if failures else 0)
