"""Finish a QR login in a private terminal; never pass 2FA through chat."""

import asyncio
import getpass
import os

from telethon import TelegramClient

from telegram_user_reader import SESSION, _credentials


async def main():
    api_id, api_hash = _credentials()
    client = TelegramClient(str(SESSION), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)
        me = await client.get_me()
        os.chmod(SESSION.with_suffix(".session"), 0o600)
        print(f"AUTHORIZED account_id={me.id}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
