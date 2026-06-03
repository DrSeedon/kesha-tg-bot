# Plan: Reverse SSH Tunnel + MCP Tool `run_on_laptop`

## Overview
1. Standalone reverse SSH tunnel (systemd unit on laptop)
2. MCP tool `run_on_laptop` in kesha_tools.py — Kesha bot can execute whitelisted commands on laptop via tunnel
3. System prompt update — tell Kesha about the laptop access capability

## Part 1: Infrastructure Setup (manual steps — user executes)

### 1.1 Install openssh-server on laptop
```bash
sudo apt install openssh-server
sudo systemctl enable --now ssh
```

### 1.2 Create dedicated tunnel user on VPS
Create a restricted user `tunnel` on VPS that can ONLY hold the reverse tunnel, no shell:
```bash
# On VPS (as root/deploy with sudo)
sudo useradd -r -s /usr/sbin/nologin -d /home/tunnel -m tunnel
sudo mkdir -p /home/tunnel/.ssh
sudo chmod 700 /home/tunnel/.ssh
```

### 1.3 Generate dedicated tunnel key on laptop
```bash
ssh-keygen -t ed25519 -f ~/.ssh/tunnel_vps -N "" -C "reverse-tunnel-to-vps"
```

### 1.4 Install tunnel key on VPS with restrictions
Add laptop's `tunnel_vps.pub` to `/home/tunnel/.ssh/authorized_keys` with restriction prefix:
```
restrict,port-forwarding,permitlisten="127.0.0.1:2222" ssh-ed25519 AAAA... reverse-tunnel-to-vps
```
`restrict` disables everything (shell, pty, X11, agent). `port-forwarding` re-enables ONLY port forwarding. `permitlisten` limits which ports can be reverse-forwarded.

```bash
sudo chown -R tunnel:tunnel /home/tunnel/.ssh
sudo chmod 600 /home/tunnel/.ssh/authorized_keys
```

### 1.5 systemd unit for tunnel on laptop
File: `/etc/systemd/system/ssh-tunnel-vps.service`
```ini
[Unit]
Description=Reverse SSH tunnel to VPS (port 2222)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=maxim
ExecStart=/usr/bin/ssh -N -R 127.0.0.1:2222:localhost:22 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -i /home/maxim/.ssh/tunnel_vps tunnel@72.56.235.40
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Flags explained:
- `-N` — no remote command, just tunnel
- `-R 2222:localhost:22` — reverse forward VPS:2222 → laptop:22
- `ServerAliveInterval=30` + `ServerAliveCountMax=3` — detect dead connection in 90s
- `ExitOnForwardFailure=yes` — exit if port 2222 already taken (avoids silent failure)
- `Restart=always` + `RestartSec=10` — systemd reconnects on any failure

### 1.6 Enable and start
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ssh-tunnel-vps
```

### 1.7 Test from VPS
```bash
# On VPS
ssh -p 2222 maxim@localhost  # should land on laptop
```

## Part 2: MCP Tool `run_on_laptop` (code changes)

### 2.1 kesha_tools.py — add `run_on_laptop` tool

**Location:** After the `react` tool, before `kesha_server = create_sdk_mcp_server(...)`.

**Design:**
- Whitelist of allowed command prefixes (safety)
- Runs `ssh -p 2222 -i /home/kesha/.ssh/tunnel_laptop maxim@localhost '<command>'` from VPS
- Timeout: 30s default, configurable per-call
- Returns stdout/stderr + exit code

**Command whitelist (exact command templates, NOT prefix match):**

Validation approach (per Codex review — prefix match is bypassable):
1. Split command into argv via `shlex.split()`
2. Check `argv[0]` against allowed binaries
3. For commands with fixed args, match the full template
4. Block shell metacharacters: `;`, `|`, `&&`, `||`, `$`, backtick, `>`, `<` in raw command string
5. Use `shlex.quote()` on the ENTIRE command for the local shell, but the remote shell still interprets it — so metachar blocking is the real defense

```python
LAPTOP_ALLOWED_COMMANDS = {
    "systemctl": ["restart orchestra", "status orchestra", "stop orchestra", "start orchestra"],
    "journalctl": True,    # any args (--no-pager -u orchestra -n 100, etc.)
    "ps": True,
    "df": True,
    "free": True,
    "uptime": True,
    "cat": True,
    "ls": True,
    "head": True,
    "tail": True,
    "grep": True,
    "find": True,
    "docker": ["ps", "logs"],
    "ss": True,
    "ip": ["addr"],
    "ping": True,          # args validated: must contain -c
    "curl": True,
    "uname": True,
    "who": True,
    "w": True,
}

SHELL_METACHAR_RE = re.compile(r"[;|&$`><]")
```

Validation logic:
```python
def _validate_laptop_cmd(cmd: str) -> str | None:
    """Returns error message if command is not allowed, None if OK."""
    if SHELL_METACHAR_RE.search(cmd):
        return "Shell metacharacters not allowed"
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return f"Invalid command syntax: {e}"
    if not argv:
        return "Empty command"
    binary = argv[0]
    if binary not in LAPTOP_ALLOWED_COMMANDS:
        return f"Command '{binary}' not whitelisted"
    allowed = LAPTOP_ALLOWED_COMMANDS[binary]
    if allowed is True:
        return None  # any args OK
    # allowed is a list of permitted subcommand patterns
    rest = " ".join(argv[1:])
    if not any(rest.startswith(sub) for sub in allowed):
        return f"Subcommand not allowed: {binary} {rest}"
    return None
```

**Note on sudoers:** `systemctl restart orchestra` requires sudo on laptop. Add sudoers entry:
```
# /etc/sudoers.d/kesha-tunnel
maxim ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart orchestra, /usr/bin/systemctl stop orchestra, /usr/bin/systemctl start orchestra, /usr/bin/systemctl status orchestra
```
And the whitelist entries for systemctl become `sudo systemctl ...` in the actual SSH command. Actually simpler: the MCP tool prepends `sudo` for systemctl commands automatically, or the whitelist includes `sudo systemctl` as the binary pattern.

**Key for kesha user on VPS:**
The kesha-bot runs as user `kesha` on VPS. Need a key that `kesha` can use to SSH to laptop through the tunnel.

Option A: Reuse `deploy`'s key (kesha has no keys).
Option B: Create a new key for `kesha` specifically.

**Recommendation:** Option B — create `/home/kesha/.ssh/tunnel_laptop` key. Add its pubkey to laptop's `maxim` authorized_keys with command restriction `command="/usr/local/bin/tunnel-cmd-validator"` or simpler: just rely on MCP whitelist since the key is only used by bot code.

Actually simpler: the bot runs as `kesha` but we can use any key. Create the key, authorize it on laptop. The MCP tool's whitelist is the security layer — the SSH key itself has normal access since it's going to the laptop (trusted environment).

### 2.2 SSH key for kesha→laptop
```bash
# On VPS as root/deploy
sudo -u kesha mkdir -p /home/kesha/.ssh
sudo -u kesha ssh-keygen -t ed25519 -f /home/kesha/.ssh/tunnel_laptop -N "" -C "kesha-bot-to-laptop"
# Copy pubkey to laptop authorized_keys
cat /home/kesha/.ssh/tunnel_laptop.pub  # add to laptop ~/.ssh/authorized_keys
```

### 2.3 Tool implementation
```python
LAPTOP_SSH_CMD = "ssh -p 2222 -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/kesha/.ssh/tunnel_laptop maxim@localhost"

@tool("run_on_laptop", "Execute a command on the laptop via reverse SSH tunnel. Whitelisted commands only.", {"command": str, "timeout": int})
async def run_on_laptop(args):
    chat_id = _require_chat()
    if isinstance(chat_id, dict):
        return chat_id
    cmd = args["command"].strip()
    timeout = min(args.get("timeout", 30) or 30, 120)
    err = _validate_laptop_cmd(cmd)
    if err:
        return {"content": [{"type": "text", "text": f"Blocked: {err}"}], "is_error": True}
    try:
        proc = await asyncio.create_subprocess_shell(
            f"{LAPTOP_SSH_CMD} {shlex.quote(cmd)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode(errors="replace")[-4000:]
        err = stderr.decode(errors="replace")[-1000:]
        result = f"exit={proc.returncode}\n"
        if out:
            result += f"stdout:\n{out}\n"
        if err:
            result += f"stderr:\n{err}"
        return {"content": [{"type": "text", "text": result.strip()}]}
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {"content": [{"type": "text", "text": f"Command timed out after {timeout}s"}], "is_error": True}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"SSH error: {e}"}], "is_error": True}
```

### 2.4 Register in kesha_server
Add `run_on_laptop` to the tools list in `create_sdk_mcp_server()`.

## Part 3: System Prompt Update

### 3.1 system_prompt.txt — add section after REACTIONS
```
LAPTOP ACCESS (run_on_laptop):
- You have SSH access to the user's laptop via reverse tunnel.
- Use run_on_laptop to execute commands on the laptop (whitelisted only).
- Common uses: restart Orchestra (systemctl restart orchestra), check logs (journalctl -u orchestra), system info (df -h, free -h, uptime).
- Timeout default 30s, max 120s. For long commands (journalctl with many lines), set a higher timeout.
- The laptop may be offline/sleeping — if command fails with SSH error, tell the user the laptop appears unreachable.
- Do NOT run destructive commands. Do NOT attempt to bypass the whitelist.
```

## Files Changed
| File | Change |
|------|--------|
| `kesha_tools.py` | Add `LAPTOP_CMD_WHITELIST`, `LAPTOP_SSH_CMD`, `run_on_laptop` tool. Add `import shlex`. Register in kesha_server. |
| `system_prompt.txt` | Add LAPTOP ACCESS section |

## What NOT to touch
- No changes to chat_state.py, handlers.py, response_stream.py, claude_session.py
- No changes to tunnel infrastructure files (those are manual setup)
- No changes to bot.py wiring (MCP tool auto-registered via kesha_server)

## Manual setup steps (user does these, NOT automated)
1. `sudo apt install openssh-server` on laptop
2. Generate tunnel keys (laptop→VPS and kesha→laptop)
3. Create tunnel user on VPS
4. Configure authorized_keys on both sides
5. Create and enable systemd unit on laptop
6. Add sudoers entry on laptop for `maxim` to restart orchestra without password
7. Test tunnel connectivity
8. Deploy code changes to VPS (`git pull && systemctl restart kesha-bot-vps`)
