#!/usr/bin/env python3
"""V-37: воспроизвести «blocked by our safety systems» вне бота.

Повторяет форму запроса Кеши (`codex_session.py`): свой CODEX_HOME с симлинком
на боевой auth.json, `thread/start` с developerInstructions + sandbox
danger-full-access + approvalPolicy never, затем один `turn/start`.
Каждый кадр app-server печатается дословно, в конце — вердикт BLOCKED/OK.
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading

BLOCK_MARK = "safety systems"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default="low")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--system-prompt", default="", help="файл с developerInstructions; пусто = без них")
    ap.add_argument("--input", required=True)
    ap.add_argument("--home", required=True, help="свежий CODEX_HOME")
    ap.add_argument("--resume-thread", default="", help="id живого треда вместо thread/start")
    ap.add_argument(
        "--share-store",
        default="",
        help="корень CODEX_HOME бота: sessions линкуется сюда, sqlite_home = этот путь",
    )
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    signal.alarm(a.timeout)

    home = pathlib.Path(a.home)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    config = f'model = "{a.model}"\nproject_doc_max_bytes = 131072\n'
    if a.share_store:
        store = pathlib.Path(a.share_store).resolve()
        config += f'sqlite_home = "{store}"\n'
        link = home / "sessions"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(store / "sessions", target_is_directory=True)
    (home / "config.toml").write_text(config)
    auth = home / "auth.json"
    if auth.is_symlink() or auth.exists():
        auth.unlink()
    auth.symlink_to(pathlib.Path.home() / ".codex" / "auth.json")

    system_prompt = ""
    if a.system_prompt:
        system_prompt = pathlib.Path(a.system_prompt).read_text()

    print(f"# model={a.model} effort={a.effort} cwd={a.cwd}")
    print(f"# system_prompt={a.system_prompt or '<none>'} ({len(system_prompt)} bytes)")
    print(f"# home={home} mcp=<none>")
    print(f"# input={a.input!r}")
    agents = pathlib.Path(a.cwd) / "AGENTS.md"
    print(f"# AGENTS.md in cwd: {agents.stat().st_size if agents.exists() else 'нет'}")
    sys.stdout.flush()

    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    proc = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=a.cwd,
        env=env,
        text=True,
        bufsize=1,
    )

    def drain_stderr():
        for line in proc.stderr:
            print(f"[stderr] {line.rstrip()}")
            sys.stdout.flush()

    threading.Thread(target=drain_stderr, daemon=True).start()

    counter = [0]
    notifications: list[dict] = []

    def send(payload: dict) -> None:
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

    def request(method: str, params: dict) -> dict:
        counter[0] += 1
        rid = counter[0]
        print(f">> {method} {json.dumps({k: v for k, v in params.items() if k != 'developerInstructions'}, ensure_ascii=False)}")
        sys.stdout.flush()
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("app-server closed stdout")
            print(f"<< {line.rstrip()[:2000]}")
            sys.stdout.flush()
            msg = json.loads(line)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]
            if msg.get("method"):
                notifications.append(msg)

    request("initialize", {"clientInfo": {"name": "kesha", "title": "Kesha", "version": "1"}})
    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    start_params = {
        "cwd": a.cwd,
        "model": a.model,
        "approvalPolicy": "never",
        "sandbox": "danger-full-access",
    }
    if system_prompt:
        start_params["developerInstructions"] = system_prompt
    if a.resume_thread:
        thread = request("thread/resume", {**start_params, "threadId": a.resume_thread})
    else:
        thread = request("thread/start", start_params)
    thread_id = (thread.get("thread") or {}).get("id")
    print(f"# thread={thread_id}")

    turn = request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": a.input}],
            "model": a.model,
            "effort": a.effort,
        },
    )
    turn_id = (turn.get("turn") or {}).get("id")

    verdict = "NO_TERMINAL_EVENT"
    raw: list[str] = []
    while True:
        if notifications:
            msg = notifications.pop(0)
        else:
            line = proc.stdout.readline()
            if not line:
                verdict = "APP_SERVER_EXITED"
                break
            print(f"<< {line.rstrip()[:2000]}")
            sys.stdout.flush()
            raw.append(line)
            msg = json.loads(line)
            if not msg.get("method"):
                continue
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if method == "turn/completed":
            t = params.get("turn") or {}
            if t.get("status") == "failed" or t.get("error"):
                verdict = "FAILED"
            else:
                verdict = "OK"
            break
        if method == "turn/failed":
            verdict = "FAILED"
            break

    blob = "".join(raw)
    if BLOCK_MARK in blob:
        verdict = f"BLOCKED ({verdict})"
    print(f"# VERDICT: {verdict} turn={turn_id}")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
