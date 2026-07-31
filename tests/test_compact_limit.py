import asyncio
from pathlib import Path

import pytest

import claude_session
from claude_session import ClaudeSession
from compact import compact_session


RAW_LIMIT = "You've hit your monthly spend limit · resets 2:20pm (Europe/Berlin)"
VALID_SUMMARY = """OBJECTIVE
- Continue safely.
USER FACTS AND PREFERENCES
- None.
DECISIONS
- Keep the session.
FILES AND ARTIFACTS
- None.
COMMANDS AND TOOL OUTCOMES
- None.
PENDING AND BLOCKERS
- None.
TEMPORAL STATE
- None.
UNCERTAINTY AND CONFLICTS
- None.
RECENT VERBATIM
- hi
CONTINUATION
- Wait."""


class ScriptedSession(ClaudeSession):
    def __init__(self, session_file: Path, scripts):
        super().__init__(cwd=".", session_file=session_file)
        self.scripts = list(scripts)
        self.usage_calls = 0

    async def get_context_usage(self):
        self.usage_calls += 1
        return {"percentage": 29 if self.usage_calls == 1 else 4}

    async def send_message(self, text):
        script = self.scripts.pop(0)
        if callable(script):
            async for chunk in script(self):
                yield chunk
            return
        for chunk in script:
            yield chunk


class Notices:
    def __init__(self):
        self.items = []

    async def __call__(self, text, *, replace):
        self.items.append((text, replace))


def session_file(tmp_path):
    path = tmp_path / "session"
    path.write_text("sid-old")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary_script, reason",
    [
        (
            [
                {"type": "text_delta", "content": "partial"},
                {"type": "error", "kind": "usage_limit", "content": RAW_LIMIT},
            ],
            "usage_limit",
        ),
        ([{"type": "text", "content": RAW_LIMIT}], "usage_limit"),
        ([], "empty_summary"),
    ],
)
async def test_summary_failure_never_starts_replacement_or_sends_summary(
    tmp_path, summary_script, reason
):
    path = session_file(tmp_path)
    session = ScriptedSession(path, [summary_script])
    notices = Notices()

    result = await compact_session(session, notify=notices)

    assert result["ok"] is False
    assert result["reason"] == reason
    assert session.session_id == "sid-old"
    assert path.read_text() == "sid-old"
    assert session._session_replacement is None
    assert all("📋" not in text for text, _ in notices.items)
    assert notices.items[0][1] is True
    assert notices.items[-1][1] is True
    assert "контекст сохранён" in notices.items[-1][0]


@pytest.mark.asyncio
async def test_preamble_limit_rolls_back_old_sid_and_sends_no_summary(tmp_path):
    path = session_file(tmp_path)

    async def limited_candidate(session):
        session.session_id = "sid-candidate"
        yield {"type": "error", "kind": "usage_limit", "content": RAW_LIMIT}

    session = ScriptedSession(
        path,
        [[{"type": "text", "content": VALID_SUMMARY}], limited_candidate],
    )
    notices = Notices()

    result = await compact_session(session, notify=notices)

    assert result["reason"] == "usage_limit"
    assert session.session_id == "sid-old"
    assert path.read_text() == "sid-old"
    assert all("📋" not in text for text, _ in notices.items)


@pytest.mark.asyncio
async def test_summary_retry_cannot_invalidate_persisted_source_sid(tmp_path):
    path = session_file(tmp_path)

    async def retried_without_source_context(session):
        session._invalidate_session()
        session.session_id = "sid-retry-without-context"
        yield {"type": "text", "content": VALID_SUMMARY}

    session = ScriptedSession(path, [retried_without_source_context])
    notices = Notices()

    result = await compact_session(session, notify=notices)

    assert result["reason"] == "source_session_changed"
    assert session.session_id == "sid-old"
    assert path.read_text() == "sid-old"
    assert ClaudeSession(cwd=".", session_file=path).session_id == "sid-old"
    assert all("📋" not in text for text, _ in notices.items)


@pytest.mark.asyncio
async def test_cancellation_before_commit_rolls_back_sid_and_terminalizes_progress(tmp_path):
    path = session_file(tmp_path)
    candidate_started = asyncio.Event()
    blocker = asyncio.Event()

    async def blocked_candidate(session):
        session.session_id = "sid-candidate"
        candidate_started.set()
        await blocker.wait()
        if False:
            yield {}

    session = ScriptedSession(
        path,
        [[{"type": "text", "content": VALID_SUMMARY}], blocked_candidate],
    )
    notices = Notices()
    task = asyncio.create_task(compact_session(session, notify=notices))
    await candidate_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.session_id == "sid-old"
    assert path.read_text() == "sid-old"
    assert session._session_replacement is None
    assert notices.items[-1][1] is True
    assert "контекст сохранён" in notices.items[-1][0]


@pytest.mark.asyncio
async def test_cancellation_after_commit_keeps_candidate_sid(tmp_path):
    path = session_file(tmp_path)
    after_commit = asyncio.Event()
    blocker = asyncio.Event()

    async def candidate(session):
        session.session_id = "sid-candidate"
        if False:
            yield {}

    class BlockingNotices(Notices):
        async def __call__(self, text, *, replace):
            self.items.append((text, replace))
            if text.startswith("📋"):
                after_commit.set()
                await blocker.wait()

    session = ScriptedSession(
        path,
        [[{"type": "text", "content": VALID_SUMMARY}], candidate],
    )
    notices = BlockingNotices()
    task = asyncio.create_task(compact_session(session, notify=notices))
    await after_commit.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.session_id == "sid-candidate"
    assert path.read_text() == "sid-candidate"
    assert session._session_replacement is None
    assert notices.items[-1] == ("✅ Контекст сжат.", True)


@pytest.mark.asyncio
async def test_success_commits_sid_before_summary_notification(tmp_path):
    path = session_file(tmp_path)

    async def candidate(session):
        session.session_id = "sid-candidate"
        if False:
            yield {}

    class CommitCheckingNotices(Notices):
        async def __call__(self, text, *, replace):
            if text.startswith("📋"):
                assert path.read_text() == "sid-candidate"
            await super().__call__(text, replace=replace)

    session = ScriptedSession(
        path,
        [[{"type": "text", "content": VALID_SUMMARY}], candidate],
    )
    notices = CommitCheckingNotices()

    result = await compact_session(session, notify=notices)

    assert result == {
        "ok": True,
        "before_pct": 29,
        "after_pct": 4,
        "summary_chars": len(VALID_SUMMARY),
    }
    assert path.read_text() == "sid-candidate"
    assert notices.items[0][1] is True
    assert any(text.startswith("📋") and replace is False for text, replace in notices.items)
    assert notices.items[-1][1] is True


@pytest.mark.asyncio
async def test_session_replacement_is_restart_atomic_across_all_commit_points(
    tmp_path, monkeypatch
):
    path = session_file(tmp_path)
    assert ClaudeSession(cwd=".", session_file=path).session_id == "sid-old"

    during = ClaudeSession(cwd=".", session_file=path)
    transaction = during.begin_session_replacement()
    during.start_session_candidate(transaction)
    during.session_id = "sid-candidate"
    assert ClaudeSession(cwd=".", session_file=path).session_id == "sid-old"
    await during.rollback_session_replacement(transaction)

    failing = ClaudeSession(cwd=".", session_file=path)
    transaction = failing.begin_session_replacement()
    failing.start_session_candidate(transaction)
    failing.session_id = "sid-candidate"

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(claude_session.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        failing.commit_session_replacement(transaction)
    await failing.rollback_session_replacement(transaction)
    assert failing.session_id == "sid-old"
    assert ClaudeSession(cwd=".", session_file=path).session_id == "sid-old"

    monkeypatch.undo()
    committed = ClaudeSession(cwd=".", session_file=path)
    transaction = committed.begin_session_replacement()
    committed.start_session_candidate(transaction)
    committed.session_id = "sid-candidate"
    committed.commit_session_replacement(transaction)
    assert ClaudeSession(cwd=".", session_file=path).session_id == "sid-candidate"
