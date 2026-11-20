import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import antigravity_session as agy
import handlers
from runtime_registry import RuntimeBuildContext, build_runtime


def make_session(tmp_path):
    return build_runtime(
        "antigravity", RuntimeBuildContext(1, str(tmp_path), "gemini-4-neon-high", "",
                                          {}, tmp_path / "session.antigravity"),
    )


@pytest.mark.asyncio
async def test_passive_operations_and_unowned_sources_do_not_touch_mux(tmp_path, monkeypatch):
    connect = AsyncMock(side_effect=AssertionError("unexpected mux connection"))
    monkeypatch.setattr(agy.MuxClient, "connect", connect)
    session = make_session(tmp_path)
    assert await session.probe_readiness() == {"ok": True, "passive": True}
    assert await session.get_context_usage(refresh=True) is None
    await session.inject_context("saved context")
    assert make_session(tmp_path)._state["handoff"] == "saved context"
    for options in ({}, {"origin": "inbox"}, {"origin": "owned_reminder"},
                    {"interactive_user": True, "origin": "inbox"}):
        chunks = [chunk async for chunk in session.send_message("ping", **options)]
        assert chunks[0]["kind"] == "antigravity_interactive_only"
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_and_steer_share_one_session_and_do_not_emit_thoughts(tmp_path, monkeypatch):
    path = tmp_path / "mux.sock"
    monkeypatch.setattr(agy, "MUX_PATH", path)
    seen = []
    complete = asyncio.Event()

    async def stand(reader, writer):
        sid = "native-test"

        def send(**msg):
            writer.write((json.dumps(msg) + "\n").encode())

        async def receive():
            return json.loads(await reader.readline())

        def update(kind, **fields):
            send(method="session/update", params={"sessionId": sid,
                                                "update": {"sessionUpdate": kind, **fields}})

        try:
            open_msg = await receive()
            assert open_msg["params"]["origin"] == "telegram"
            send(id=open_msg["id"], result={"sessionId": sid, "audio": True})
            first = await receive()
            seen.append(first)
            send(method="session/submitted", params={"sessionId": sid})
            update("agent_thought_chunk", content={"type": "text", "text": "private"})
            update("agent_message_chunk", content={"type": "text", "text": "first"})
            cancel = await receive()
            assert cancel["method"] == "session/cancel"
            send(id=cancel["id"], result={"submitted": True})
            send(id=first["id"], result={"stopReason": "cancelled",
                                         "accounting": {"input_tokens": 10, "cost_usd": 0.1}})
            second = await receive()
            seen.append(second)
            send(method="session/submitted", params={"sessionId": sid})
            update("usage_update", used=50, size=100)
            update("agent_message_chunk", content={"type": "text", "text": "second"})
            send(id=second["id"], result={"stopReason": "end_turn",
                                          "accounting": {"input_tokens": 2, "cost_usd": 0.2}})
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            complete.set()

    server = await asyncio.start_unix_server(stand, path)
    path.chmod(0o600)
    voice = tmp_path / "voice.ogg"
    voice.write_bytes(b"OggSdemo")
    session = make_session(tmp_path)
    await session.inject_context("cross-runtime context")
    try:
        chunks = []
        async for chunk in session.send_message("listen", interactive_user=True, origin="telegram",
                                                audio_paths=(str(voice),)):
            chunks.append(chunk)
            if chunk.get("content") == "first":
                assert await session.steer("follow-up", interactive_user=True)
        await asyncio.wait_for(complete.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()
    assert [item["content"] for item in chunks if item["type"] == "text_delta"] == ["first", "second"]
    assert seen[0]["params"]["blocks"][-1] == {
        "type": "audio", "mimeType": "audio/ogg",
        "data": base64.b64encode(voice.read_bytes()).decode(),
    }
    assert "cross-runtime context" in seen[0]["params"]["blocks"][0]["text"]
    assert seen[0]["params"]["sessionId"] == seen[1]["params"]["sessionId"]
    assert "follow-up" in seen[1]["params"]["blocks"][0]["text"]
    assert session.last_response_usage["input_tokens"] == 12
    assert session.last_response_usage["cost_usd"] == pytest.approx(0.3)
    assert (await session.get_context_usage())["percentage"] == 50
    reloaded = make_session(tmp_path)
    assert reloaded.session_id == "native-test"
    assert "handoff" not in reloaded._state
    session.reset_response_usage()
    assert session.last_response_usage == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,convert", [("voice", False), ("video_note", True), ("audio", True)])
async def test_native_telegram_voice_paths_never_use_deepgram(tmp_path, monkeypatch, kind, convert):
    import config
    import media

    uid = 1
    monkeypatch.setattr(handlers, "ALLOWED", {uid})
    monkeypatch.setattr(config, "DEEPGRAM", "")
    monkeypatch.setattr(handlers, "transcribe", AsyncMock(side_effect=AssertionError("Deepgram called")))
    start = SimpleNamespace(runtime_id="antigravity")
    monkeypatch.setattr(handlers, "_start_media", AsyncMock(return_value=(start, 0, 0)))
    download = AsyncMock(return_value=str(tmp_path / "incoming"))
    monkeypatch.setattr(handlers, "download_file", download)
    ogg = tmp_path / "converted.ogg"
    converter = AsyncMock(return_value=str(ogg))
    monkeypatch.setattr(media, "native_ogg", converter)
    enqueue = AsyncMock()
    monkeypatch.setattr(handlers, "enqueue", enqueue)
    from datetime import datetime, timezone
    msg = SimpleNamespace(from_user=SimpleNamespace(id=uid), chat=SimpleNamespace(id=uid),
                          caption=None, date=datetime.now(timezone.utc), message_id=1)
    setattr(msg, kind, SimpleNamespace(file_id="voice-test", file_unique_id="unique",
                                       duration=6, file_name=None))
    await getattr(handlers, f"h_{kind}")(msg)
    converter.assert_awaited_once_with(str(tmp_path / "incoming"), convert=convert)
    assert f"{ogg} (6s)" in enqueue.await_args.args[1]
    assert enqueue.await_args.kwargs["audio_paths"] == (str(ogg),)
    handlers.transcribe.assert_not_awaited()
    if kind == "voice":
        assert download.await_args.args[1].endswith(".ogg")
    if kind == "video_note":
        assert "Не открывай исходный .mp4" in enqueue.await_args.args[1]


def test_selection_and_active_runtime_persist_together(tmp_path):
    from message_log import MessageLog
    from model_choices import resolve_choice

    path = tmp_path / "chat.sqlite"
    db = MessageLog(path)
    choice = resolve_choice("antigravity", 3, "high")
    db.select_model(1, "antigravity", *choice)
    db.select_model(1, "codex", *resolve_choice("codex", 0, "medium"))
    restored = MessageLog(path)
    assert restored.get_runtime(1) == "codex"
    assert restored.get_model_selection(1, "antigravity") == ("gemini-4-hydrogen-high", None)
    assert restored.get_model_selection(1, "codex") == ("gpt-6.2-sol", "medium")


@pytest.mark.asyncio
async def test_automatic_compaction_does_not_open_mux(tmp_path, monkeypatch):
    from compact import compact_session

    connect = AsyncMock(side_effect=AssertionError("unexpected mux connection"))
    monkeypatch.setattr(agy.MuxClient, "connect", connect)
    session = make_session(tmp_path)
    assert await compact_session(session) == {"ok": False, "reason": "antigravity_interactive_only"}
    connect.assert_not_awaited()



@pytest.mark.asyncio
async def test_http_inbox_delivers_directly_for_antigravity(monkeypatch):
    import inbox_server

    state = SimpleNamespace(runtime_id="antigravity", accept_entry=AsyncMock())
    tg = AsyncMock()
    monkeypatch.setattr(inbox_server, "ALLOWED", {1})
    monkeypatch.setattr(inbox_server, "_expected_inbox_token", lambda: "secret-tok")
    monkeypatch.setattr(inbox_server, "_bot_ref", tg)
    monkeypatch.setattr(inbox_server, "_registry_ref", SimpleNamespace(get=lambda _: state))
    request = SimpleNamespace(headers={"Authorization": "Bearer secret-tok"}, json=AsyncMock(return_value={"chat_id": 1, "message": "notice"}))
    response = await inbox_server.handle_inbox(request)
    assert response.status == 200
    assert "notice" in tg.send_message.await_args.args[1]
    state.accept_entry.assert_not_awaited()



@pytest.mark.asyncio
async def test_status_uses_unknown_cost_without_mux(tmp_path, monkeypatch):
    import kesha_tools

    session = make_session(tmp_path)
    state = SimpleNamespace(session=session, debounce_sec=3)
    registry = SimpleNamespace(get=lambda _: state)
    monkeypatch.setattr(handlers, "_registry", registry)
    monkeypatch.setattr(handlers, "allowed", lambda _: True)
    monkeypatch.setattr(handlers, "media_count", lambda: 0)
    monkeypatch.setattr(handlers, "log_size", lambda: "0 B")
    monkeypatch.setattr(handlers, "_send_safe", AsyncMock())
    message = SimpleNamespace(from_user=SimpleNamespace(id=1, language_code="ru"),
                              chat=SimpleNamespace(id=1))
    await handlers.h_status(message)
    assert "n/a" in handlers._send_safe.await_args.args[1]
    monkeypatch.setattr(kesha_tools, "_resolve_chat", lambda: 1)
    monkeypatch.setattr(kesha_tools, "_bot_ref",
                        SimpleNamespace(get_session=lambda _: session, registry=registry))
    result = await kesha_tools.get_bot_status.handler({})
    assert "Context: n/a" in result["content"][0]["text"]
    assert "Session cost: $n/a" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_audio_false_stream_survives_slow_consumer_and_hides_split_file_uri(tmp_path, monkeypatch):
    session = make_session(tmp_path)

    class Stand:
        def __init__(self):
            self.submitted = asyncio.Event()
            self.session_id = None

        async def request(self, method, params, *, timeout=None, on_write=None):
            if method == "session/open":
                return {"sessionId": None, "audio": False}
            assert method == "session/prompt"
            on_write()
            assert params["turnId"]
            assert all(block["type"] == "text" for block in params["blocks"])
            assert "voice.ogg" in params["blocks"][-1]["text"]
            self.session_id = "voice-stand"
            self.attach(self.session_id)
            self.submitted.set()
            for text in ("begin fi", "le://", "/private/path", " end ") + ("x",) * 3000:
                self.update({"sessionUpdate": "agent_message_chunk",
                             "content": {"type": "text", "text": text}})
            return {"stopReason": "end_turn"}

        async def close(self):
            pass

    monkeypatch.setattr(agy.MuxClient, "connect", AsyncMock(return_value=Stand()))
    chunks = [chunk async for chunk in session.send_message(
        "[voice: /stand/voice.ogg — read_file]", interactive_user=True,
        origin="telegram", audio_paths=(str(tmp_path / "missing.ogg"),),
    )]
    assert sum(chunk["type"] == "text_delta" for chunk in chunks) == 1
    text = "".join(chunk.get("content", "") for chunk in chunks)
    assert text == "begin  end " + "x" * 3000
    assert chunks[-1]["ok"]


def test_runtime_and_handoff_rollback_together(tmp_path, monkeypatch):
    from message_log import MessageLog

    store = MessageLog(tmp_path / "chat-log.sqlite")
    store.set_runtime(1, "claude")

    def reject(*args):
        raise RuntimeError("simulated DB refusal")

    with monkeypatch.context() as patch:
        patch.setattr(store, "set_runtime", reject)
        with pytest.raises(RuntimeError):
            store.adopt_runtime(1, "antigravity", selection=("chosen", None), handoff="private note")
    assert store.get_runtime(1) == "claude"
    assert store.pending_handoff(1, "antigravity") is None
    assert store.get_model_selection(1, "antigravity") is None
    store.adopt_runtime(1, "antigravity", selection=("chosen", None), handoff="private note")
    operation, note = store.pending_handoff(1, "antigravity")
    assert note == "private note"
    assert store.get_runtime(1) == "antigravity"
    store.consume_handoff(1, "antigravity", operation)
    assert store.pending_handoff(1, "antigravity") is None
