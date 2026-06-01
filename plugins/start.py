import os
import shutil
import psutil
import asyncio
from time import time

from pyrogram import Client, filters
from pyrogram.types import Message

from database.db import db
from helpers.files import get_readable_file_size, get_readable_time
from config import ADMINS

BOT_START_TIME = time()


@Client.on_message(filters.private & filters.command("start"))
async def start(client: Client, message: Message):
    if not await db.is_user_exist(message.from_user.id):
        await db.add_user(message.from_user.id, message.from_user.first_name)

    session = await db.get_session(message.from_user.id)
    dest, dest_label = await db.get_destination(message.from_user.id)

    status = "✅ Logged in" if session else "❌ Not logged in — use /login"
    dest_text = f"📤 **{dest_label}**" if dest else "📤 **This chat**"

    # Check for resumable batches
    paused = await db.get_active_batches(message.from_user.id)
    paused_line = ""
    if paused:
        paused_line = f"\n\n🔁 You have **{len(paused)}** paused batch(es) — use /resume to continue."

    await message.reply(
        f"👋 **Hi {message.from_user.mention}!**\n\n"
        "I save restricted content from any Telegram channel or group — "
        "including **topic threads**, with **server-side fast copy** for "
        "unrestricted sources.\n\n"
        f"**Account:** {status}\n"
        f"**Destination:** {dest_text}"
        f"{paused_line}\n\n"
        "**Quick start:**\n"
        "• Send any post link → download it\n"
        "• `link1 - link2` → batch range\n"
        "• /clone → clone an entire channel/group\n"
        "• /playlist → send multiple links at once\n"
        "• /setchannel → send files to a channel/group\n"
        "• /resume → continue paused batches\n"
        "• /cancel → stop your current task\n"
        "• /help → full help",
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await message.reply(
        "📖 **Full Help**\n\n"
        "**Single post:**\n"
        "`https://t.me/channel/123`\n\n"
        "**Batch range (same chat):**\n"
        "`https://t.me/channel/100 - https://t.me/channel/200`\n\n"
        "**Private channel:**\n"
        "`https://t.me/c/1234567890/123`\n\n"
        "**Group topic:**\n"
        "`https://t.me/c/1234567890/5/123`\n"
        "`https://t.me/groupusername/5/123`\n\n"
        "**Topic batch:**\n"
        "`https://t.me/c/123/5/100 - https://t.me/c/123/5/200`\n\n"
        "**Playlist (many links at once):**\n"
        "Use /playlist → send all links, one per line\n"
        "Each line can be a single link or a range\n\n"
        "**Clone entire channel/group:**\n"
        "`/clone https://t.me/channelname`\n"
        "`/clone https://t.me/c/1234567890`\n"
        "`/clone @channelname`\n"
        "Finds first → last message automatically\n\n"
        "**Destination:**\n"
        "/setchannel — send downloads to a channel/group\n"
        "/destination — show current destination\n"
        "/resetdest — reset to this chat\n\n"
        "**Controls:**\n"
        "/cancel — stop your task (progress saved!)\n"
        "/resume — continue paused batches\n"
        "/status — check task status\n"
        "/login /logout — manage your account\n\n"
        "**Speed:**\n"
        "Unrestricted sources use server-side copy (instant).\n"
        "Restricted sources are downloaded & re-uploaded.",
        disable_web_page_preview=True,
    )


@Client.on_message(filters.private & filters.forwarded)
async def handle_forwarded(client: Client, message: Message):
    """Reply with chat ID + ready /clone command when user forwards a message."""
    chat = getattr(message, "forward_from_chat", None)
    if not chat:
        # Forwarded from a user, not a channel/group — ignore silently
        return

    chat_id = chat.id
    chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)

    # Build a usable reference for /clone
    if chat.username:
        clone_ref = f"https://t.me/{chat.username}"
    elif str(chat_id).startswith("-100"):
        raw_id = str(chat_id)[4:]   # strip the -100 prefix
        clone_ref = f"https://t.me/c/{raw_id}"
    else:
        clone_ref = str(chat_id)

    await message.reply(
        f"<b>📋 Chat identified</b>\n\n"
        f"<b>Name:</b> {chat_name}\n"
        f"<b>ID:</b> <code>{chat_id}</code>\n\n"
        f"<b>Clone this chat:</b>\n"
        f"<code>/clone {clone_ref}</code>",
        quote=True,
    )



async def stats(client: Client, message: Message):
    uptime = get_readable_time(time() - BOT_START_TIME)
    total_users = await db.total_users_count()

    def _sys():
        t, _, f = shutil.disk_usage(".")
        return (
            get_readable_file_size(t), get_readable_file_size(f),
            psutil.cpu_percent(interval=0.5),
            psutil.virtual_memory().percent,
            round(psutil.Process(os.getpid()).memory_info()[0] / 1024 ** 2),
        )

    disk_total, disk_free, cpu, ram, proc_mem = await asyncio.to_thread(_sys)

    await message.reply(
        f"📊 **Bot Stats**\n\n"
        f"⏱ Uptime: `{uptime}`\n"
        f"👥 Total users: `{total_users}`\n"
        f"💾 Disk: `{disk_free}` free of `{disk_total}`\n"
        f"🖥 CPU: `{cpu}%` | RAM: `{ram}%`\n"
        f"🤖 Bot mem: `{proc_mem} MiB`"
    )


@Client.on_message(filters.private & filters.command("logs") & filters.user(ADMINS))
async def logs(client: Client, message: Message):
    if os.path.exists("logs.txt"):
        await message.reply_document("logs.txt", caption="📋 Logs")
    else:
        await message.reply("No log file found.")
