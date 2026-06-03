# Research: Reverse SSH Tunnel (Laptop ← VPS)

## Goal
Reverse SSH tunnel so that from VPS (`72.56.235.40`) you can `ssh localhost -p 2222` and land on the laptop.
Scheme: `[Laptop] ---ssh -R 2222:localhost:22--→ [VPS]`

## Current State

### Laptop (Ubuntu, user `maxim`)
- **sshd: NOT running, openssh-server NOT installed** — `ss -tlnp | grep :22` empty, `dpkg -l | grep openssh-server` empty
- **SSH key:** `~/.ssh/id_ed25519` (ed25519, comment `parsehub-timeweb`)
- **SSH config:** none (`~/.ssh/config` does not exist)
- **autossh:** not installed
- **Laptop public key** already in VPS `deploy` authorized_keys — can SSH to VPS as `deploy`

### VPS (Ubuntu 24.04, `72.56.235.40`)
- **sshd:** running on port 22 (0.0.0.0)
- **Users:**
  - `deploy` (uid 1001) — has SSH keys, used for deployment
  - `kesha` (uid 1001) — runs kesha-bot, code at `/opt/kesha-bot`. **No .ssh directory**
- **SSH config (`/etc/ssh/sshd_config`):**
  - `AllowTcpForwarding` — commented out (defaults to `yes` — OK)
  - `GatewayPorts` — commented out (defaults to `no` — tunnel binds to localhost only, which is what we want)
  - `ClientAliveInterval` — commented out (defaults to 0 — no keepalive from server)
  - `ClientAliveCountMax` — commented out (defaults to 3)
- **autossh:** not installed on VPS (but not needed there — tunnel originates from laptop)
- **deploy authorized_keys:** 3 keys (laptop `parsehub-timeweb`, github-actions-deploy, gha-seedon-deploy)

## Analysis

### What needs to happen
1. **Install openssh-server on laptop** — sshd must accept connections for the tunnel to work
2. **Tunnel originates from laptop** → connects to VPS → opens port 2222 on VPS localhost
3. **On VPS:** `ssh -p 2222 maxim@localhost` → routed through tunnel → arrives at laptop sshd

### autossh vs systemd restart
| Approach | Pros | Cons |
|----------|------|------|
| **autossh** | Purpose-built, monitors tunnel, auto-reconnects | Extra package, another moving part |
| **systemd + ssh -R + Restart=always** | No extra deps, systemd handles restarts, `ServerAliveInterval` for keepalive | Slightly less sophisticated reconnect |
| **cloudflared tunnel** | Works through NAT/firewalls, no sshd needed | Cloudflare dependency, more complex setup |

**Recommendation:** systemd unit with `ssh -R` + `ServerAliveInterval/ServerAliveCountMax` + `Restart=always`. Simpler, no extra packages beyond openssh-server. autossh adds monitoring port overhead for what systemd `Restart=always` already handles.

### Security considerations
1. **Tunnel-only user on VPS** — NOT needed. The tunnel is initiated FROM laptop TO VPS using existing `deploy` user. VPS just needs `AllowTcpForwarding yes` (default). No new user on VPS required.
2. **Laptop sshd hardening:**
   - Key-only auth (disable password)
   - Listen on localhost or limit to specific interfaces
   - Actually: sshd listens on 0.0.0.0:22 by default, but laptop is behind NAT — not exposed to internet. The tunnel port 2222 on VPS binds to localhost only (`GatewayPorts no`), so only someone on VPS can use it.
3. **Dedicated key for tunnel** — generate a new key pair on laptop specifically for the tunnel service (not reuse the main key). Restricts blast radius.
4. **Restrict tunnel key on VPS** — in `deploy`'s `authorized_keys`, can add `command="/bin/false",no-pty,no-X11-forwarding,permitopen="none"` prefix to the tunnel key so it can ONLY hold the tunnel, not get a shell.

### Why would Kesha bot need laptop access?
Open question — the tunnel gives VPS→laptop SSH access, but the task doesn't specify what for. Possible uses:
- Run commands on laptop from Kesha (MCP tool?)
- Access local files/resources from VPS
- Development workflow: push from laptop, trigger on VPS
- Just general "remote access to laptop when away from home"

This doesn't affect the tunnel setup itself.

### Port choice
- 2222 — standard alt SSH port, no conflicts. Checked VPS: nothing on 2222.

## Risks
1. **Laptop offline/sleeping** — tunnel drops. systemd `Restart=always` reconnects when network is back, but laptop must be awake.
2. **Laptop hibernates** — tunnel dies, systemd can't reconnect until wake. Consider `systemd-inhibit` or power settings.
3. **VPS reboot** — tunnel survives (laptop keeps retrying). No action needed on VPS side.
4. **sshd not installed on laptop** — MUST install `openssh-server` first. This is a prerequisite.

## Prerequisites (require user action)
1. `sudo apt install openssh-server` on laptop
2. Optionally `sudo apt install autossh` (if we go that route — I recommend not)
3. SSH key generation for tunnel service
4. Editing VPS `authorized_keys` (or user does it manually)
