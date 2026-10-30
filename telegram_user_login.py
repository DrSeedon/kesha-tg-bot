"""Interactive one-time Telegram login. Run locally as the bot's OS user.

Credentials must already be in storage/telegram-user/credentials (mode 0600):
API_ID=<integer> and API_HASH=<value>. This script never prints either value.
"""

import asyncio
import getpass
import os

from telethon import TelegramClient, errors

from telegram_user_reader import PRIVATE, SESSION, _credentials


async def main():
    if PRIVATE.stat().st_mode & 0o077:
        raise RuntimeError("Private directory has unsafe permissions")
    api_id, api_hash = _credentials()
    client = TelegramClient(str(SESSION), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            phone = input("Telegram phone number (international format): ").strip()
            sent = await client.send_code_request(phone)
            code = input("Code from Telegram: ").strip()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            except errors.SessionPasswordNeededError:
                password = getpass.getpass("Telegram 2FA password: ")
                await client.sign_in(password=password)
        me = await client.get_me()
        os.chmod(SESSION.with_suffix(".session"), 0o600)
        print(f"Authorized Telegram account id={me.id}; session saved with mode 0600")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
