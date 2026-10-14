"""V-43 spike: does an undrained message stream starve the control channel?

Real SDK `Query` (0.2.158) over a fake transport. The CLI side emits N frames
(as a notification turn with include_partial_messages would) and answers every
control_request right after them. Nobody consumes `receive_messages()` between
turns — exactly the bot's state after `send_message` broke out on its Result.
"""

import asyncio
import json
import sys

from claude_agent_sdk._internal.query import Query


class FakeTransport:
    def __init__(self, backlog: int):
        self.q: asyncio.Queue = asyncio.Queue()
        for i in range(backlog):
            self.q.put_nowait({"type": "stream_event", "uuid": f"e{i}", "session_id": "s",
                               "event": {"type": "content_block_delta"}})

    async def connect(self):
        pass

    async def write(self, data: str):
        msg = json.loads(data)
        if msg.get("type") == "control_request":
            self.q.put_nowait({"type": "control_response", "response": {
                "subtype": "success", "request_id": msg["request_id"],
                "response": {"totalTokens": 1}}})

    async def read_messages(self):
        while True:
            yield await self.q.get()

    async def close(self):
        pass

    def is_ready(self):
        return True

    async def end_input(self):
        pass


async def trial(backlog: int, drain: bool) -> str:
    q = Query(FakeTransport(backlog), is_streaming_mode=True)
    await q.start()
    drainer = None
    if drain:
        async def consume():
            async for _ in q.receive_messages():
                pass
        drainer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    try:
        await asyncio.wait_for(q.get_context_usage(), timeout=2)
        verdict = "answered"
    except Exception as exc:
        verdict = f"TIMEOUT ({type(exc).__name__})"
    if drainer:
        drainer.cancel()
    await q.close()
    return verdict


async def main():
    for backlog, drain in [(50, False), (99, False), (100, False), (101, False), (500, False), (500, True)]:
        print(f"backlog={backlog:4d} drained={drain!s:5}: {await trial(backlog, drain)}", flush=True)


asyncio.run(main())
sys.exit(0)
