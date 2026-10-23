<p align="center">
  <img src="banner.png" alt="Kesha TG Bot" width="100%">
</p>

<h1 align="center">Kesha TG Bot</h1>
<h3 align="center">A full Claude Code (or Codex) agent that lives in your Telegram chat</h3>

<p align="center">
  <sub><b>English</b> · <a href="README.ru.md">Русский</a></sub>
</p>

<p align="center">
  <a href="#what-you-get">What You Get</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#how-a-message-travels">How It Works</a> ·
  <a href="#features">Features</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#what-it-does-and-does-not-do">Limits</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/github/license/DrSeedon/kesha-tg-bot" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
  <img src="https://img.shields.io/badge/telegram-aiogram%203-2CA5E0" alt="aiogram 3">
</p>

---

**Kesha is a single-node Telegram bot that gives each chat its own persistent agent session.** Under the hood it is the official `claude-agent-sdk` (the Claude Code CLI) with a second, manually switched runtime on the Codex CLI. You talk to it like you would in a Claude Code terminal: it reads files, runs commands, calls MCP servers and remembers the conversation, but the interface is a Telegram chat with voice notes, photos, live-streaming answers and reminders.

The bot is built around one working directory (`WORK_DIR`): your notes, `CLAUDE.md`, `.mcp.json` and every tool the agent uses live there. There is no database of "knowledge" to maintain: the agent searches your files and your chat history as plain text.

## What You Get

| Capability | Current status |
|---|---|
| Persistent agent per chat | One session per chat and runtime, stored on disk, resumed after a bot restart. Chats never share state. |
| Two runtimes | Claude (default) and Codex, switched by hand with `/runtime`. No automatic failover. |
| Streaming answers | The reply is sent once and edited live; a separate bubble shows tool calls with timers. |
| Voice, photos, files | Voice notes and video notes are transcribed (Deepgram, optional); photos, albums, documents, audio and video are downloaded and handed to the agent as files. |
| Image generation | Uses the optional hub image tool, announces the wait (about a minute) and sends the result as a photo. One paid Nano Banana fallback attempt if the hub is unavailable or fails. |
| Messages while it works | A message sent mid-turn is steered into the running Claude turn, like typing into Claude Code. On Codex it waits for the next turn. |
| Context management | The context is measured before every message; the bot compacts it itself before the window fills. |
| Memory | Chat history is searched as text (`search_memory`, `read_messages`); your notes are searched with `rg`/Grep. No embeddings, no index to rebuild. |
| Reminders | Three kinds, including ones that wake the agent to do work at a set time. |
| Background agents | Subagents started by the CLI can finish after the answer; their results arrive in the chat on their own. |
| Limits card | `/limits` shows subscription windows, but only if you run the companion Orchestra service. |
| Safety | Whitelist of users, sandboxed file sending, authenticated tool bridge. The agent itself runs with full permissions. |

This is a capability summary, not a guarantee that every optional integration (Deepgram, Codex, Orchestra) is configured on your host.

## Quick Start

**Requirements:** Python 3.12+, Git, a Telegram bot token from [@BotFather](https://t.me/BotFather), and a logged-in Claude Code account on the host (the SDK drives the Claude Code CLI with the credentials in `~/.claude`). Optional: `ffmpeg` (video notes), a Deepgram key (voice transcription), the Codex CLI logged in on the host (second runtime).

```bash
git clone https://github.com/DrSeedon/kesha-tg-bot.git
cd kesha-tg-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

claude auth login        # once, as the user that will run the bot

cp .env.example .env     # then edit it: TELEGRAM_BOT_TOKEN, ALLOWED_USERS
python bot.py
```

`python setup_wizard.py` asks for the same values interactively and writes `.env`. Leave `CLAUDE_MODEL` unset to use the code default (see [Configuration](#configuration)); the example file and the wizard still carry older model names.

**Set `ALLOWED_USERS`.** If it is empty, the bot answers anyone who finds it, and the agent can run commands on your machine. Put your Telegram user id there (the bot tells an unknown user their id when it refuses them).

Open the bot in Telegram, send `/start`, then write anything.

### Run as a service

`kesha-bot.service` is a template with the author's user and paths: copy it, change `User`, `WorkingDirectory` and `ExecStart`, then:

```bash
sudo cp kesha-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kesha-bot
```

The `/restart` command and the `restart_bot` tool call `sudo systemctl restart <unit>` with unit names that are written into the source (`handlers.py`, `kesha_tools.py`). Either allow that call without a password for your unit name and edit the names, or do not use those two.

## How a Message Travels

```
Telegram ─► aiogram ─► handlers.py ─► ChatState (per chat) ─► runtime ─► Claude Code CLI
                                          │                       └────► codex app-server
                                          │ debounce, batching
                                          ▼
                              response_stream.py ─► live edits + tool bubble in Telegram
                                          ▲
        MCP tools (kesha_tools.py) ───────┘   in-process for Claude,
                                              unix-socket bridge for Codex
```

1. A handler wraps the message with a prefix the agent can read: time, `msg_id`, sender, forward and reply markers (`[2026-01-01 12:00 +0700] [msg_id=7] [Name (@user)]: [reply: "…"] text`). The clock in that prefix is fixed to UTC+7.
2. `ChatState` collects messages for a debounce window (default 3 s, `/debounce`, 0–30 s), waits for pending transcriptions, and a photo without a caption waits up to 10 s for the voice note that usually follows.
3. Before the turn, the runtime measures the context. If the batch would cross the compaction threshold, the bot compacts first.
4. The reply streams into one Telegram message that is edited in place; tool calls appear in their own bubble. The finished text is converted to Telegram entities.
5. Every message and every answer is written to `storage/messages.db`, together with a per-answer usage row (tokens, cache split, cost where the runtime reports it).

Each chat is a small state machine (`idle → collecting → processing → compacting / stopping`), and all per-chat mutation goes through it, so two users never touch each other's session.

## Features

### 💬 Chat and media

- **Text, replies, forwards.** Links hidden behind text are written out as `anchor (url)`; forwards and replies reach the agent as `[Forwarded from …]` / `[reply: "…"]`.
- **Voice and video notes.** Transcribed with Deepgram (Nova‑3, language set to Russian, results cached by file id). Without a key the agent receives the file path instead. Video notes go through `ffmpeg` first.
- **Photos, albums, documents, audio, video.** Downloaded under their original names into `MEDIA_DIR` and passed as `[photo: path]`, `[document: path (name)]` and so on; the agent opens them with its own tools. Albums are grouped into one message. A file that cannot be downloaded is reported as "file too large". Downloads are cached by Telegram's unique file id.
- **Stickers** are passed as their emoji. Anything unrecognised, including Telegram's rich messages, reaches the agent as text instead of being dropped.
- **Interface language** (Russian or English) follows the user's Telegram language. The command menu is registered in both. Some system notices (compaction progress, the `/limits` caption, the start greeting) are Russian only.

### ⚡ Streaming that survives Telegram's limits

- The first chunk is sent immediately; later chunks edit the same message while the text is plain, and the final text is re-rendered with Markdown entities.
- One edit budget per chat (about one edit per 3.1 s) keeps all live bubbles under Telegram's edit ceiling. When Telegram returns a flood-control error the bot switches to sending a fresh message and deleting the old one, so the answer never freezes.
- **Tool bubble.** Each tool call shows an icon, a short hint, and a live timer; a "still working" hint appears after 60 s; the bubble closes when answer text starts, so message order stays correct. Up to 20 tools are listed.
- **`/stop`** interrupts the turn, keeps the partial text, and stops the CLI's background agents with it (it tells you how many).
- **Step limit.** A turn is capped at 150 tool cycles; hitting the cap triggers one automatic "continue" and then asks you to say "continue".
- **Failure messages are specific.** Subscription limit (with the reset time when the runtime knows it), full context, a tool call the model could not format, an unavailable saved session, and a provider safety refusal each get their own text. Quota and context errors are never blindly retried; other transient errors recreate the session up to twice.
- **Safety refusal rollback.** If Anthropic's filter rejects a message, the bot forks the session to just before that message so the flagged text cannot poison later turns.

### 🧠 Context: measured, admitted, compacted

- **Admission.** Before every batch the bot asks the runtime how full the context is and adds the size of the incoming text. At 88 % of the window on Claude (90 % on Codex) it compacts first, then sends. If the context cannot be measured the message is sent anyway, because the provider is the one that should refuse.
- **After a heavy turn** the context is measured again, so one huge tool output cannot carry the chat into the wall unnoticed.
- **Claude compaction** is the bot's own transaction (`compact.py`): the model writes a ten-section handoff (objective, decisions, files, commands, pending work, uncertainty, last user messages and so on); the text is validated, secrets are redacted, the last three user messages are appended verbatim by code, and the session is replaced atomically. Any failure rolls back to the old session. The summary is posted in the chat. During this step the model is also told to update an existing note in the working directory if a durable fact came up. If the context is already too full for a summary, the CLI's own compactor is used as a fallback.
- **Codex compaction** is native (`thread/compact/start`) and keeps the thread.
- **`/compact`** runs it by hand (Claude needs at least 16 000 free tokens, Codex 12 000); `/clear` starts a clean session.
- **Background agents are respected.** Compacting or clearing would kill them, so an automatic compaction waits until they finish, and `/clear` tells you how many it stopped.

### 🔀 Two runtimes

| | Claude | Codex |
|---|---|---|
| Backend | `claude-agent-sdk` → Claude Code CLI | `codex app-server --stdio` (JSON‑RPC) |
| Default model | `CLAUDE_MODEL` (`claude-opus-5-5`) | `KESHA_CODEX_MODEL` (`gpt-6-sol`) |
| Mid-turn messages | steered into the running turn | wait for the next turn |
| Compaction | the bot's own handoff transaction | native |
| Context percentage | yes | after the first reported turn |
| Cost reporting | yes | no |
| Background agents | yes | no |
| Resume after restart | yes | yes |

- `/runtime` shows the current runtime, its model, and buttons; `/runtime codex`, `/claude` and `/codex` switch. A switch is refused while a turn is running.
- The replacement runtime is started and probed before the old one is retired. On success the last visible messages (up to 40 messages, 24 000 characters) are handed over; on failure the chat stays where it was and says so.
- Each chat keeps a separate session file per runtime (`storage/sessions/<chat_id>` and `.codex`), so switching back resumes that runtime's own history.
- Codex runs with a private `CODEX_HOME` and only the MCP servers the bot gives it, not the ones from your personal Codex config. It works with `approvalPolicy=never` and `danger-full-access`.
- The switch is for emergencies (a banned or throttled subscription). Claude is the default and the only runtime that has seen long production use.
- The probe also reads the provider's quota windows: switching to a runtime whose subscription is at 100 % is refused. During normal turns the provider's own limit error is what stops a turn, and the bot reports it with the reset time.

### 🧰 MCP tools

The bot's own tools are served to Claude in-process and to Codex through a local unix-socket bridge (`tool_bridge.py`, with a launcher in `kesha_mcp_proxy.py`).

| Tool | What it does |
|---|---|
| `send_photo`, `send_file`, `send_video`, `send_audio`, `send_voice` | Send a file to the chat. Paths are checked against a whitelist (see [Security](#-security)). |
| `react` | Put emoji reactions on messages by `msg_id`. |
| `create_reminder`, `list_reminders`, `update_reminder`, `cancel_reminder` | Reminder CRUD ([below](#-reminders)). |
| `search_memory`, `read_messages` | Search the chat history and read messages in full ([below](#-memory-as-plain-text)). |
| `set_debounce`, `toggle_debug`, `get_bot_status`, `restart_bot` | The agent can tune and inspect the bot itself. |
| `run_on_laptop` | Whitelisted diagnostics over a reverse SSH tunnel. Author-specific; Claude only. |

External MCP servers are read from `~/.claude.json`, `~/.claude/settings.json` and `WORK_DIR/.mcp.json` (first source wins) and handed to the CLI explicitly. Edit one and run `/reload`: the CLI restarts with the new set and the conversation is kept; the reply lists added and removed servers.

### 🔎 Memory as plain text

Kesha has two stores and searches both the way a developer searches code.

- **Chat history.** `search_memory` runs a case-insensitive regex over this chat's messages and returns `[id | time | role] snippet`, newest first, with the match count. It takes `pattern`, `role`, `since`/`until` dates and `limit` (default 10, at most 30). `read_messages(id, before, after)` returns a message in full with its neighbours. Each call opens its own read-only connection.
- **Notes.** The agent greps and reads the files in `WORK_DIR` with its normal tools. The system prompt teaches the method: guess two to four words the original text probably used, search by stems and alternations such as `фиолетов|сиренев`, narrow by date (dates are in file names), then read the file before answering. If your `CLAUDE.md` holds a "topic → file" index, the agent uses it too.
- **Why not embeddings.** Earlier versions ran a hybrid vector index (bge‑m3 + FTS5). On 35 rephrased questions to Kesha's own history and notes, an agent with text search answered correctly 83 % of the time against 87 % with the vector index (a difference inside the noise), and the vector index alone found the right record in the top 5 for 49 %. The index, its model, the file watcher and three dependencies were removed. Details in the changelog and `.orchestra/tasks/V-45/report.md`.

### ⏰ Reminders

Stored in `storage/reminders.db`, checked every 30 s, delivered also after downtime.

| Type | Behaviour |
|---|---|
| `plain` | Sends the text as an alarm at the due time, no model involved. |
| `urgent_llm` | At the due time the agent gets the reminder as a task and does it: check something, read something, write to you in its own words. |
| `lazy_llm` | Stays silent until you next write, then is injected into that prompt. If it waits longer than 24 h it is promoted to `urgent_llm`. |

Reminders can repeat (`30m`, `2h`, `1d`, `1w`, `3mo`), be aligned to a time of day, or run as an on/off cycle (for example 60 days on, 30 days off, with a different text for each phase). Reminders missed while the bot was down are delivered on start: plain ones as one digest, urgent ones as one task. Times are stored in UTC; the "time of day" alignment and the prompt clock use UTC+7.

### 🤖 Background work and mid-turn messages

- **Messages while it works.** On Claude, what you send during a turn is glued with the same debounce, passes the same context admission, and is written into the running turn through the CLI, the way Claude Code treats text typed mid-run. The answer comes in the same reply. If admission refuses, the message waits and goes out as the next batch.
- **Background agents.** When the CLI finishes work on its own (a subagent or a background command reports back), the bot renders that turn in the chat without waiting for you. The bot tracks how many background agents are open and avoids actions that would kill them.
- **Long tools.** Silence is judged by what the CLI is doing: 2 minutes without output while it writes text, 5 minutes after a tool call, and up to 30 minutes while a tool or background task is still open (a long build is not a hang). A stalled turn is cut and retried.
- **HTTP inbox.** A local endpoint `POST http://127.0.0.1:18081/inbox` (`{"message": …, "sender": …, "chat_id": …}`) lets another program on the same host push a message into a chat; it is injected as a prompt and mirrored to Telegram. It listens on loopback only and has no token, so anything on that host can use it.

### 📊 `/limits` (optional)

`/limits` forwards a card and caption from the companion [Orchestra](https://github.com/DrSeedon/orchestra) service on the same host: Claude, Codex, Spark and Grok subscription windows, weekly headroom and burn pace. It works in private chats only, needs `ORCHESTRA_URL` and an Orchestra token, and reports an error if the service is not there. The rest of the bot does not need it.

### 🔐 Security

- **Users.** `ALLOWED_USERS` is the only access control; everything else is checked against it. A refused user is told their id once.
- **Outgoing files.** Every `send_*` tool resolves the path first and accepts it only inside `KESHA_SENDABLE_ROOTS` (default `./storage:./artifacts:/tmp/kesha`). The file is opened at check time without following symlinks, hard links must stay inside the roots too, and the size is capped at 50 MB. This closes `..`, symlink, hardlink and swap-after-check escapes.
- **Tool bridge (Codex).** A unix socket with a token; chat addressing is held by the server under a random, expiring session handle (one hour, revoked at turn end or runtime switch), so a model cannot choose another chat. Argument names are normalised and checked against each tool's own schema, which rejects `chat_id` smuggling in any spelling. `run_on_laptop` is never exposed there.
- **`run_on_laptop`.** A command whitelist with per-binary rules (no shell metacharacters, no `find -exec`, no `curl` uploads, no `kill -1`, read-only `ip`). It is tied to a specific SSH tunnel and key location in the source: adapt it or leave it unused.
- **Secrets.** Compaction summaries are scanned for private keys, common token formats and `password=` style assignments before they are stored. The external MCP config is written to a file with restricted permissions.
- **What is not sandboxed.** The agent runs with full permissions: the Claude CLI in `bypassPermissions` mode with every tool auto-approved, Codex in `danger-full-access`. Treat anyone in `ALLOWED_USERS` as having a shell on the host.

## Commands

| Command | Description |
|---|---|
| `/start` | Bot status: session, model, working directory, debounce |
| `/help` | Command reference |
| `/status` | Model, session, uptime, context usage, session cost, media and log size |
| `/limits` | Subscription windows card from Orchestra (private chats, optional) |
| `/clear` | New session; refused while a turn is running |
| `/compact` | Compact the context now; postponed until a running turn or background agents finish |
| `/runtime` | Show the runtime and model, with switch buttons; `/runtime claude\|codex` switches |
| `/claude`, `/codex` | Shortcuts for the two switches |
| `/stop` | Interrupt the current answer (not in the command menu, but registered) |
| `/ping` | Session id check |
| `/debounce <sec>` | Batching delay, 0–30 s |
| `/debug` | Toggle debug logging to `logs/kesha.log` |
| `/reload` | Restart the CLI to pick up changed MCP servers, keeping the conversation |
| `/restart` | Restart the bot service (see [Run as a service](#run-as-a-service)) |

## Configuration

Set in `.env` (read at start).

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token | **required** |
| `ALLOWED_USERS` | Telegram user ids, comma-separated; empty means everyone | empty |
| `CLAUDE_MODEL` | Claude model id | `claude-opus-5-5` |
| `WORK_DIR` | Working directory of the agent: notes, `CLAUDE.md`, `.mcp.json` | `.` |
| `KESHA_RUNTIME` | Startup runtime: `claude` or `codex` | `claude` |
| `KESHA_CODEX_MODEL` | Model for the Codex runtime | `gpt-6-sol` |
| `KESHA_CODEX_BIN` | Path to the `codex` binary | auto-detected |
| `KESHA_CODEX_HOME` | Private `CODEX_HOME` for the bot | per-session dir |
| `KESHA_SENDABLE_ROOTS` | Colon-separated roots files may be sent from | `./storage:./artifacts:/tmp/kesha` |
| `KESHA_BRIDGE_SOCKET` | Unix socket of the Codex tool bridge | `./storage/bridge.sock` |
| `DEEPGRAM_API_KEY` | Voice and video-note transcription | unset (files only) |
| `DEBOUNCE_SEC` | Default batching delay | `3` |
| `PHOTO_CAPTION_WAIT_SEC` | Wait for a caption after a bare photo | `10` |
| `PHOTO_CAPTION_REPLY_SEC` | Short wait once the caption arrived | `1` |
| `NOTIFY_CHAT` | Chat for system notices | first of `ALLOWED_USERS` |
| `MEDIA_DIR` | Downloaded media | `./storage/media` |
| `MEDIA_MAX_MB` | Size budget of the media directory: when over it, files older than 24 h are deleted oldest first (checked at start and daily) | `100` |
| `LOG_DIR` | Logs (rotated daily, 7 kept) | `./logs` |
| `DEBUG` | Debug logging | `false` |
| `TG_PROXY` / `HTTPS_PROXY` | Proxy for the Telegram connection | unset |
| `ORCHESTRA_URL`, `ORCHESTRA_ENV_FILE` | Companion service for `/limits` | local defaults |

State lives under `storage/`: `messages.db` (chat log, activity, usage), `reminders.db`, `sessions/`, `media/`, `bridge.sock`, `mcp-external.json`.

## Architecture

| File | Role |
|---|---|
| `bot.py` | Bootstrap, singleton lock, wiring, reminder loop, inbox server |
| `config.py` | Environment, logging, UI strings (ru/en), MCP server loading |
| `handlers.py` | Message and command handlers, command menu |
| `chat_state.py` | Per-chat state machine: debounce, steering, admission, compaction, runtime switching, background turns |
| `response_stream.py` | Streaming to Telegram, edit budget, retries, terminal notices |
| `tool_status.py` | Live tool bubble |
| `runtime_protocol.py`, `runtime_registry.py` | The runtime contract and a registry that fails at start if a backend misstates its capabilities |
| `claude_session.py` | Claude backend: persistent CLI connection, steering, context measurement, session replacement |
| `codex_session.py` | Codex backend over `codex app-server` |
| `compact.py` | Claude handoff compaction |
| `kesha_tools.py`, `tool_bridge.py`, `kesha_mcp_proxy.py` | The MCP tools, the Codex bridge and its stdio launcher |
| `memory_search.py` | Text search over the chat history |
| `reminders.py`, `message_log.py`, `media.py`, `telegram_io.py` | Reminders, SQLite log, downloads/transcription, Telegram helpers |
| `file_access.py` | Outgoing file whitelist |
| `limits.py`, `quota_gate.py` | `/limits` client, quota admission |
| `inbox_server.py` | Local HTTP inbox |
| `system_prompt.txt` | The agent's Telegram context, formatting and memory rules |

## Stack

- Python 3.12+, aiogram 3 with aiogram-media-group, telegramify-markdown
- `claude-agent-sdk` (bundles the Claude Code CLI); Codex CLI optional
- SQLite for the chat log and reminders
- Deepgram (speech to text) and `ffmpeg` (video notes), both optional

## What It Does and Does Not Do

- **Single node, no failover.** One process per bot token. Runtime switching is manual.
- **Codex is the fallback, not an equal.** No mid-turn steering, no background agents, no cost data, and the context percentage only after its first turn.
- **Built for one household.** The prompt clock, `repeat_at_time` and the logs use UTC+7; some notices are Russian only; `run_on_laptop` and the service unit name assume the author's setup. Change them in the source if they do not fit.
- **Text search is literal.** It does not match synonyms for you: the agent has to guess the words. In the measurement above it needed about 1.5 more search calls per answer than the vector index, with the same token cost.
- **A pathological regex from the model occupies a worker thread until it ends** (the pattern length is capped at 300 characters).
- **`/limits` needs a companion service**, and the HTTP inbox has no authentication beyond loopback.

## Contributing and License

Changes are tracked in [CHANGELOG.md](CHANGELOG.md); open follow-ups are in [TODO.md](TODO.md). Run the tests with `python -m pytest tests -q` in an environment built from `requirements.txt` plus `pytest` and `pytest-asyncio`; the Claude SDK and `mcp` versions should match the host.

Released under the [MIT License](LICENSE).
