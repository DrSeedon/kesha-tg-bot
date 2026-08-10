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
