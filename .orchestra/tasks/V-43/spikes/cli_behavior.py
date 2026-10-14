"""V-43 spike: how the persistent CLI behaves around background agents.

Questions (answers go to ../research.md):
 Q1 does get_context_usage answer while the CLI runs its own turn / is idle with bg work?
 Q2 with --replay-user-messages, when is our prompt echoed, and is our uuid kept?
 Q3 what `origin` do results of notification turns and of our prompts carry?
 Q4 what happens to a prompt sent during a notification turn / mid tool call?
A reader drains the stream the whole time, so backpressure is NOT under test here.
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk._internal.message_parser import parse_message  # noqa: F401

OUT = Path(__file__).with_suffix(".log.jsonl")
T0 = time.monotonic()
events: list[dict] = []


def rec(kind: str, **data):
    row = {"t": round(time.monotonic() - T0, 2), "kind": kind, **data}
    events.append(row)
    print(json.dumps(row, ensure_ascii=False)[:400], flush=True)


def summarize(raw: dict) -> dict:
    t = raw.get("type")
    out = {"type": t, "subtype": raw.get("subtype"), "uuid": raw.get("uuid")}
    if raw.get("origin"):
        out["origin"] = raw["origin"]
    if t == "stream_event":
        ev = raw.get("event", {})
        out["ev"] = ev.get("type")
        return out
    if t in ("user", "assistant"):
        msg = raw.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            out["text"] = c[:160]
        elif isinstance(c, list):
            out["blocks"] = [
                (b.get("type"), (b.get("text") or b.get("name") or str(b.get("content"))[:80])[:120])
                for b in c
            ]
        out["parent_tool_use_id"] = raw.get("parent_tool_use_id")
        for k in ("isReplay", "is_replay", "isSynthetic", "priority"):
            if k in raw:
                out[k] = raw[k]
    if t == "result":
        for k in ("num_turns", "stop_reason", "terminal_reason", "is_error", "result"):
            out[k] = raw.get(k) if k != "result" else str(raw.get(k))[:120]
    if t == "system":
        for k in ("task_id", "status", "task_type", "summary"):
            if k in raw:
                out[k] = str(raw[k])[:120]
    return out


async def reader(client: ClaudeSDKClient):
    # Read raw dicts so unknown fields (origin, replay flags) are visible.
    async for raw in client._query.receive_messages():
        s = summarize(raw)
        if s["type"] == "stream_event" and s.get("ev") not in ("message_start", "message_stop"):
            continue
        rec("msg", **s)


async def send(client: ClaudeSDKClient, text: str, tag: str) -> str:
    uid = str(uuid.uuid4())

    async def one():
        yield {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "uuid": uid,
        }

    await client.query(one())
    rec("sent", tag=tag, uuid=uid, text=text[:80])
    return uid


async def probe(client: ClaudeSDKClient, tag: str):
    start = time.monotonic()
    try:
        usage = await asyncio.wait_for(client.get_context_usage(), timeout=20)
        rec("probe", tag=tag, ok=True, secs=round(time.monotonic() - start, 2),
            total=usage.get("totalTokens"))
    except Exception as exc:
        rec("probe", tag=tag, ok=False, secs=round(time.monotonic() - start, 2), err=repr(exc)[:120])


def seen(pred) -> bool:
    return any(pred(e) for e in events)


async def wait_for(pred, timeout: float, label: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if seen(pred):
            return True
        await asyncio.sleep(0.2)
    rec("timeout", label=label)
    return False


async def main():
    work = Path(__file__).parent / "work"
    work.mkdir(exist_ok=True)
    options = ClaudeAgentOptions(
        model=sys.argv[1] if len(sys.argv) > 1 else "haiku",
        cwd=str(work),
        permission_mode="bypassPermissions",
        include_partial_messages=True,
        max_turns=30,
        setting_sources=[],
        extra_args={"replay-user-messages": None},
        env={"DISABLE_AUTO_COMPACT": "1"},
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    rd = asyncio.create_task(reader(client))
    n_results = lambda: sum(1 for e in events if e.get("type") == "result")

    # A: launch one background agent and end the turn.
    await send(client, (
        "Use the Agent tool with run_in_background=true to start exactly ONE "
        "general-purpose subagent whose only job is to run the bash command "
        "`sleep 25; echo bg-done` and report its output. Do not wait for it. "
        "Then reply with the single word LAUNCHED and end your turn."
    ), "A")
    await wait_for(lambda e: e.get("type") == "result", 120, "A result")
    await asyncio.sleep(1)
    await probe(client, "idle_with_bg_agent")

    # C: wait for the notification turn to begin, then talk into it.
    await wait_for(lambda e: e.get("type") == "system" and e.get("subtype") == "task_notification", 120, "notification")
    await asyncio.gather(
        probe(client, "during_notification_turn"),
        send(client, "Question B: what is 2+2? Answer with just the number.", "B"),
    )
    base = n_results()
    await wait_for(lambda e: False, 20, "settle after B")  # let everything flush
    rec("info", results_after_B=n_results() - base)

    # D: foreground long tool; probe and inject mid-call.
    await send(client, "Run the bash command `sleep 15; echo fg-done`, then reply OK-D.", "D")
    await asyncio.sleep(6)
    await asyncio.gather(
        probe(client, "mid_foreground_tool"),
        send(client, "Question E: also tell me the word BANANA.", "E"),
    )
    await wait_for(lambda e: False, 45, "settle after D/E")

    rd.cancel()
    await client.disconnect()
    OUT.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")


asyncio.run(main())
