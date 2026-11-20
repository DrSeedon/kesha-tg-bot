"""Pure-Python Out-of-Band control panel (/orch) & secret redaction for Orchestra."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values

ORCHESTRA_DIR = Path(os.getenv("KESHA_ORCHESTRA_ROOT", str(Path.home() / "orchestra")))
ORCHESTRA_DB_PATH = Path(os.getenv("ORCHESTRA_DB_PATH", str(ORCHESTRA_DIR / "data/orchestra.db")))
ORCHESTRA_ENV_PATH = Path(os.getenv("ORCHESTRA_ENV_FILE", str(ORCHESTRA_DIR / ".env")))
KESHA_ENV_PATH = Path(os.getenv("KESHA_ENV_FILE", "/opt/kesha-bot/.env"))

_TOKEN_PATTERNS = (
    re.compile(r"\bbot\d{6,15}:[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),
)
_SECRET_KEY_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|COOKIE|AUTH)", re.IGNORECASE)
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,80}$")


def _literal_secrets() -> list[str]:
    vals: list[str] = []
    for path in (ORCHESTRA_ENV_PATH, KESHA_ENV_PATH):
        try:
            if path.is_file():
                for k, v in dotenv_values(path).items():
                    if v and len(v.strip()) >= 8 and _SECRET_KEY_RE.search(str(k)):
                        vals.append(v.strip())
        except Exception:
            pass
    return vals


def mask_secrets(text: str, *, extra_secrets: Iterable[str] = ()) -> str:
    """Redact credentials, Bearer headers, TG/OpenAI/GitHub tokens, and .env secrets."""
    out = str(text or "")
    for secret in (*_literal_secrets(), *extra_secrets):
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[REDACTED]")
    for idx, pat in enumerate(_TOKEN_PATTERNS):
        if idx == 3:
            out = pat.sub(r"\1[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


async def _run_cmd(*argv: str, timeout: float = 10.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out_b.decode(errors="replace").strip(), err_b.decode(errors="replace").strip()


def _read_db_session_counts(db_path: Path | None = None) -> tuple[dict[str, int], list[tuple[str, str]]]:
    db_path = db_path or ORCHESTRA_DB_PATH
    counts = {"running": 0, "waiting": 0, "idle": 0}
    running: list[tuple[str, str]] = []
    if not db_path.is_file():
        return counts, running
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0) as conn:
        for st, cnt in conn.execute(
            "SELECT status, COUNT(*) FROM sessions WHERE status IN ('running','waiting','idle') GROUP BY status"
        ).fetchall():
            if st in counts:
                counts[st] = int(cnt)
        for name, model in conn.execute(
            "SELECT name, model FROM sessions WHERE status='running' ORDER BY created_at DESC LIMIT 20"
        ).fetchall():
            running.append((str(name), str(model)))
    return counts, running


async def _systemd_show(unit: str) -> dict[str, str]:
    rc, out, _ = await _run_cmd(
        "systemctl", "show", unit,
        "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "MemoryCurrent", "-p", "NRestarts",
    )
    info: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
    return info


async def _wait_unit_active(unit: str = "orchestra.service", timeout_sec: float = 45.0) -> tuple[bool, dict[str, str]]:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        rc, out, _ = await _run_cmd("systemctl", "is-active", unit, timeout=5.0)
        if rc == 0 and out.strip() == "active":
            return True, await _systemd_show(unit)
        await asyncio.sleep(1.0)
    return False, await _systemd_show(unit)


async def orch_status() -> str:
    svc, sock = await asyncio.gather(
        _systemd_show("orchestra.service"),
        _systemd_show("orchestra.socket"),
    )
    active_state = svc.get("ActiveState", "unknown")
    sub_state = svc.get("SubState", "?")
    pid = svc.get("MainPID", "0")
    restarts = svc.get("NRestarts", "?")
    mem_raw = svc.get("MemoryCurrent", "")
    try:
        mem_mb = f"{int(mem_raw) / (1024 * 1024):.0f} MiB" if mem_raw and mem_raw != "[not set]" else "n/a"
    except ValueError:
        mem_mb = "n/a"

    _, head_sha, _ = await _run_cmd("git", "-C", str(ORCHESTRA_DIR), "rev-parse", "--short", "HEAD")
    lkg_rc, lkg_sha, _ = await _run_cmd("git", "-C", str(ORCHESTRA_DIR), "rev-parse", "--short", "last-known-good")
    lkg_label = lkg_sha if lkg_rc == 0 else "нет"

    counts, _ = _read_db_session_counts()
    api_note = "db"
    # Strictly avoid hitting :8888 when inactive so socket-activation won't wake a stopped/crashed service.
    if active_state == "active":
        try:
            import aiohttp
            import limits
            token = limits.orchestra_token()
            timeout = aiohttp.ClientTimeout(total=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    limits._orchestra_url("/api/sessions"),
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status == 200:
                        rows = await resp.json()
                        if isinstance(rows, list):
                            counts = {
                                st: sum(1 for r in rows if r.get("status") == st)
                                for st in ("running", "waiting", "idle")
                            }
                            api_note = "live"
        except Exception:
            pass

    return "\n".join([
        f"🛠 Orchestra Status ({api_note})",
        f"• service: {active_state} ({sub_state}), PID={pid}, RAM={mem_mb}, restarts={restarts}",
        f"• socket: {sock.get('ActiveState', 'unknown')} ({sock.get('SubState', '?')})",
        f"• git: HEAD={head_sha or '?'} | last-known-good={lkg_label}",
        f"• sessions: running={counts['running']}, waiting={counts['waiting']}, idle={counts['idle']}",
    ])


async def orch_logs(n: int = 35) -> str:
    count = max(1, min(80, int(n)))
    rc, out, err = await _run_cmd(
        "sudo", "-n", "journalctl", "-u", "orchestra.service", "-n", str(count), "--no-pager",
        timeout=10.0,
    )
    body = out if rc == 0 else f"journalctl error ({rc}): {err or out}"
    clean = mask_secrets(body)
    if len(clean) > 3600:
        clean = "...\n" + clean[-3590:]
    return f"📜 orchestra.service (last {count}):\n{clean}"


async def orch_restart(*, force: bool = False) -> str:
    _, running = _read_db_session_counts()
    if running and not force:
        items = ", ".join(f"{name} ({model})" for name, model in running)
        return (
            f"⚠ Сейчас выполняются ходы ({len(running)}): {items}.\n"
            "Для принудительного рестарта: /orch restart force"
        )
    rc, out, err = await _run_cmd("sudo", "-n", "systemctl", "restart", "orchestra.service", timeout=30.0)
    if rc != 0:
        return f"❌ Ошибка restart ({rc}): {mask_secrets(err or out)}"
    ok, svc = await _wait_unit_active("orchestra.service", 45.0)
    _, head_sha, _ = await _run_cmd("git", "-C", str(ORCHESTRA_DIR), "rev-parse", "--short", "HEAD")
    return (
        f"{'✅' if ok else '❌'} Orchestra restart: {svc.get('ActiveState', '?')} "
        f"PID={svc.get('MainPID', '?')} HEAD={head_sha or '?'}"
    )


async def orch_rollback(ref: str = "last-known-good") -> str:
    target = (ref or "last-known-good").strip()
    if not _REF_RE.fullmatch(target) or target.startswith("-"):
        return "❌ Недопустимый git ref"
    rc_rev, resolved_sha, err_rev = await _run_cmd(
        "git", "-C", str(ORCHESTRA_DIR), "rev-parse", "--short", target
    )
    if rc_rev != 0:
        return f"❌ Ref '{target}' не найден: {mask_secrets(err_rev)}"
    _, dirty_before, _ = await _run_cmd("git", "-C", str(ORCHESTRA_DIR), "status", "--porcelain")
    rc_reset, out_reset, err_reset = await _run_cmd(
        "git", "-C", str(ORCHESTRA_DIR), "reset", "--keep", target, timeout=15.0
    )
    if rc_reset != 0:
        return (
            f"❌ Откат на {target} ({resolved_sha}) отменён (грязные файлы мешают безопасному reset --keep): "
            f"{mask_secrets(err_reset or out_reset)}"
        )
    rc_rst, out_rst, err_rst = await _run_cmd(
        "sudo", "-n", "systemctl", "restart", "orchestra.service", timeout=30.0
    )
    if rc_rst != 0:
        return f"❌ Git откачен на {resolved_sha}, но restart упал: {mask_secrets(err_rst or out_rst)}"
    ok, svc = await _wait_unit_active("orchestra.service", 45.0)
    dirty_note = f" (untracked/dirty preserved: {len(dirty_before.splitlines())})" if dirty_before else ""
    return (
        f"{'✅' if ok else '❌'} Orchestra rollback → {target} ({resolved_sha}){dirty_note}: "
        f"{svc.get('ActiveState', '?')} PID={svc.get('MainPID', '?')}"
    )


async def handle_orch_command(text: str) -> str:
    parts = (text or "").strip().split()
    sub = parts[1].lower() if len(parts) > 1 else "status"
    arg = parts[2] if len(parts) > 2 else ""
    if sub in ("status", "st"):
        return await orch_status()
    if sub in ("logs", "log"):
        try:
            n = int(arg) if arg else 35
        except ValueError:
            n = 35
        return await orch_logs(n)
    if sub == "restart":
        return await orch_restart(force=(arg.lower() == "force"))
    if sub == "rollback":
        return await orch_rollback(arg or "last-known-good")
    return (
        "Использование /orch:\n"
        "• /orch status — состояние сервиса, сокета, HEAD, тега и сессий\n"
        "• /orch logs [N] — последние N (1..80, дефолт 35) строк лога (с маской секретов)\n"
        "• /orch restart [force] — рестарт (проверяет running сессии)\n"
        "• /orch rollback [ref] — безопасный откат на last-known-good или ref"
    )
