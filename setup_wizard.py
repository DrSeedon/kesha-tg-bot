"""Interactive setup wizard for Kesha TG Bot."""

import sys
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
EXAMPLE = Path(__file__).parent / ".env.example"


def ask(prompt: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if not val and default:
            return default
        if not val and required:
            print("  This field is required.")
            continue
        if val:
            return val
        return default


def main():
    print()
    print("=== Kesha TG Bot — Setup Wizard ===")
    print()

    if ENV_FILE.exists():
        ans = input(".env already exists. Overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted.")
            sys.exit(0)

    token = ask("Telegram Bot Token (from @BotFather)", required=True)
    users = ask("Allowed user IDs (comma-separated, empty = all)", default="")
    model = ask("Claude model", default="claude-sonnet-4-6")
    work_dir = ask("Working directory for Claude", default=".")
    deepgram = ask("Deepgram API key (for voice transcription, optional)", default="")
    debug = ask("Enable debug mode?", default="false")

    lines = [
        f"TELEGRAM_BOT_TOKEN={token}",
        f"ALLOWED_USERS={users}",
        f"CLAUDE_MODEL={model}",
        f"WORK_DIR={work_dir}",
        f"DEEPGRAM_API_KEY={deepgram}",
        f"DEBUG={debug}",
        f"MEDIA_DIR=./storage/media",
        f"LOG_DIR=./logs",
    ]

    ENV_FILE.write_text("\n".join(lines) + "\n")
    print()
    print(f".env written to {ENV_FILE}")
    print()
    print("Next steps:")
    print("  1. pip install -r requirements.txt")
    print("  2. python bot.py")
    print()


if __name__ == "__main__":
    main()
