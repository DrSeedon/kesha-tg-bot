import asyncio
import time
from types import SimpleNamespace

from tool_status import ToolStatusTracker


class FakeBot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def test_failed_tool_is_never_rendered_as_done():
    bot = FakeBot()
    tracker = ToolStatusTracker(bot, None, 42)
    tracker.status_msg = SimpleNamespace(message_id=7)
    tracker.tools = [{
        "name": "mail_count_all",
        "icon": "📧",
        "hint": "",
        "start": time.time() - 300,
        "end": None,
    }]
    tracker._current_idx = 0
    tracker._last_text = "🤖 *Работаю...*"

    result = asyncio.run(tracker.fail())

    assert result == 7
    rendered = bot.edits[-1][0]
    assert "*Прервано:*" in rendered
    assert "⚠️ 📧 mail\\_count\\_all" in rendered
    assert "*Сделано:*" not in rendered
    assert "✅ 📧 mail\\_count\\_all" not in rendered


def test_failed_status_keeps_already_finished_tools_successful():
    tracker = ToolStatusTracker(FakeBot(), None, 42)
    now = time.time()
    tracker.tools = [
        {"name": "mail_list", "icon": "📧", "hint": "", "start": now - 2, "end": now - 1},
        {"name": "mail_read", "icon": "📧", "hint": "", "start": now - 1, "end": now},
    ]
    tracker._current_idx = 1

    rendered = tracker._render_text(failed=True)

    assert "✅ 📧 mail\\_list" in rendered
    assert "⚠️ 📧 mail\\_read" in rendered


def test_completed_tool_timer_stops_before_model_thinks(monkeypatch):
    tracker = ToolStatusTracker(FakeBot(), None, 42)
    tracker.status_msg = SimpleNamespace(message_id=7)
    tracker.tools = [{
        "name": "Bash", "icon": "🖥", "hint": "", "start": 100.0, "end": None,
    }]
    tracker._current_idx = 0
    monkeypatch.setattr("tool_status.time.time", lambda: 100.3)

    asyncio.run(tracker.complete_current())

    assert tracker.tools[0]["end"] == 100.3
    assert tracker._current_idx is None
    assert "· 0s" in tracker.bot.edits[-1][0]
