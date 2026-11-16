"""Run a real CLI resume and stdio MCP call against an isolated reminder DB.

No model turn or Telegram message is sent. Run with the production dependency
pins from AGENTS.md. All credentials and session state stay in a temporary dir.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import kesha_tools
import tool_bridge
from codex_session import CodexSession
from reminders import ReminderDB


async def main():
    with tempfile.TemporaryDirectory(prefix='bridge-probe-', dir=ROOT / '.orchestra/tasks/V-53') as directory:
        root = Path(directory)
        tool_bridge.SOCKET_PATH = root / 'bridge.sock'
        token = tool_bridge.issue_token()
        db = ReminderDB(root / 'reminders.db')
        kesha_tools._rem._db = db
        bridge = tool_bridge.ToolBridge(token, kesha_tools.get_current_chat)
        item = kesha_tools.create_reminder
        bridge.register(item.name, item.handler,
                        allowed_args=kesha_tools.tool_arg_names(item))
        session = CodexSession(str(root), chat_id=42, session_file=root / 'session',
                               mcp_servers={'kesha': {'type': 'sdk'}})

        async def call(handle):
            params = StdioServerParameters(command=sys.executable,
                args=[str(ROOT / 'kesha_mcp_proxy.py')], env={
                    **os.environ, 'KESHA_BRIDGE_TOKEN': token,
                    'KESHA_BRIDGE_SESSION': handle,
                    'KESHA_BRIDGE_SOCKET': str(tool_bridge.SOCKET_PATH),
                    'KESHA_NO_FILE_LOG': '1'})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    return await client.call_tool('create_reminder', {
                        'text': 'probe', 'when_iso': '2030-01-01T00:00:00+00:00',
                        'type': 'plain', 'repeat_interval': '', 'repeat_at_time': '',
                        'cycle_on_days': 0, 'cycle_off_days': 0, 'text_off': ''})

        await bridge.start()
        try:
            await session._connect()
            await session.inject_context('Isolated lifecycle probe; no user task.')
            first_pid, thread, old = session._proc.pid, session.session_id, session._bridge_session
            chat, _, runtime = tool_bridge._SESSIONS[old]
            tool_bridge._SESSIONS[old] = (chat, time.monotonic() - 1, runtime)
            await session._connect()
            assert session.session_id == thread and session._proc.pid != first_pid
            assert session._bridge_session != old
            rejected = await call(old)
            assert rejected.isError and 'unknown session' in rejected.content[0].text
            accepted = await call(session._bridge_session)
            assert not accepted.isError, accepted
            rows = db.list_for(42)
            assert len(rows) == 1 and rows[0]['text'] == 'probe'
            assert tool_bridge.session_chat(old) is None
            print('PASS: real app-server restarted; same thread resumed; stale stdio proxy denied; fresh stdio proxy created one reminder in isolated chat/DB')
        finally:
            await session.safe_disconnect()
            await bridge.stop()
            db.conn.close()
            kesha_tools._rem._db = None


if __name__ == '__main__':
    asyncio.run(main())
