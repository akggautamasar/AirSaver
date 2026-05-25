import asyncio
import time
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid
from pyrogram.types import Message
from database.db import db
from config import ADMINS


@Client.on_message(filters.command("broadcast") & filters.user(ADMINS) & filters.reply)
async def broadcast(bot: Client, message: Message):
    b_msg = message.reply_to_message
    users = await db.get_all_users()
    total = await db.total_users_count()

    sts = await message.reply(f"📢 Broadcasting to {total} users...")
    start = time.time()
    success = blocked = deleted = failed = done = 0

    async for user in users:
        uid = user.get("id")
        if not uid:
            done += 1
            failed += 1
            continue
        try:
            await b_msg.copy(chat_id=uid)
            success += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await b_msg.copy(chat_id=uid)
                success += 1
            except Exception:
                failed += 1
        except UserIsBlocked:
            await db.delete_user(uid)
            blocked += 1
        except InputUserDeactivated:
            await db.delete_user(uid)
            deleted += 1
        except (PeerIdInvalid, Exception):
            failed += 1

        done += 1
        if done % 25 == 0:
            try:
                await sts.edit(
                    f"📢 Broadcasting...\n"
                    f"{done}/{total} done | ✅ {success} | 🚫 {blocked} | 🗑 {deleted} | ❌ {failed}"
                )
            except Exception:
                pass

    elapsed = time.strftime("%M:%S", time.gmtime(time.time() - start))
    await sts.edit(
        f"✅ **Broadcast complete in {elapsed}**\n\n"
        f"Total: {total} | Sent: {success} | "
        f"Blocked: {blocked} | Deleted: {deleted} | Failed: {failed}"
    )
