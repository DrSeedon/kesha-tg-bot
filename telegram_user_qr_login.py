"""One-time QR login without exchanging the owner's login code in chat."""

import asyncio
import os
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors

from telegram_user_reader import PRIVATE, SESSION, _credentials


QR_IMAGE = Path(__file__).resolve().parent / "storage" / "media" / "telegram-user-login.png"


async def main():
    if PRIVATE.stat().st_mode & 0o077:
        raise RuntimeError("Private directory has unsafe permissions")
    api_id, api_hash = _credentials()
    client = TelegramClient(str(SESSION), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            qr_login = await client.qr_login()
            qrcode.make(qr_login.url).save(QR_IMAGE)
            os.chmod(QR_IMAGE, 0o600)
            print(f"QR_READY {QR_IMAGE}", flush=True)
            try:
                await qr_login.wait(timeout=120)
            except errors.SessionPasswordNeededError:
                print("TWO_FACTOR_PASSWORD_REQUIRED", flush=True)
                return
            except asyncio.TimeoutError:
                print("QR_EXPIRED", flush=True)
                return
        me = await client.get_me()
        os.chmod(SESSION.with_suffix(".session"), 0o600)
        print(f"AUTHORIZED account_id={me.id}", flush=True)
    finally:
        QR_IMAGE.unlink(missing_ok=True)
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
