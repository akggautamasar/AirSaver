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
        "• /clone → clone an entire channel/group/topic\n"
        "• /playlist → send multiple links at once\n"
        "• /setchannel → send files to a channel/group\n"
        "• /resume → continue paused batches\n"
        "• /cancel → stop your current task\n"
        "• /help → full help\n\n"
        "💡 **Tip:** Forward any message from a private group to me — "
        "I'll tell you its ID and the exact /clone command.",
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
        "`/clone @channelname`\n\n"
        "**Clone a specific topic thread only:**\n"
        "`/clone https://t.me/c/1234567890/5/1`\n"
        "`/clone https://t.me/groupname/5/1`\n"
        "_(the number after the group ID is the topic ID)_\n\n"
        "**Get ID of a private group:**\n"
        "Forward any message from it to me — I reply with the ID\n"
        "and the ready /clone command.\n\n"
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
    """Show chat ID + /clone command when user forwards a message from any chat."""
    chat = getattr(message, "forward_from_chat", None)
    if not chat:
        return  # forwarded from a user, not a channel/group

    chat_id = chat.id
    chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)

    # Build the base link for /clone
    if chat.username:
        base_ref = f"https://t.me/{chat.username}"
    elif str(chat_id).startswith("-100"):
        raw_id = str(chat_id)[4:]
        base_ref = f"https://t.me/c/{raw_id}"
    else:
        base_ref = str(chat_id)

    # Try to include the forwarded message ID so the user can see the full link
    fwd_msg_id = getattr(message, "forward_from_message_id", None)
    msg_link_line = ""
    if fwd_msg_id:
        msg_link_line = (
            f"\n<b>Forwarded message link:</b>\n"
            f"<code>{base_ref}/{fwd_msg_id}</code>\n"
        )

    topic_section = (
        f"\n<b>📌 For topic groups:</b>\n"
        f"Right-click the original message → <b>Copy Link</b>\n"
        f"If the link looks like:\n"
        f"<code>https://t.me/c/ID/TOPIC/MSGID</code>\n"
        f"Clone that topic with:\n"
        f"<code>/clone {base_ref}/TOPIC/MSGID</code>\n"
        f"<i>(replace TOPIC and MSGID with the actual numbers)</i>"
    )

    await message.reply(
        f"<b>📋 Chat identified</b>\n\n"
        f"<b>Name:</b> {chat_name}\n"
        f"<b>ID:</b> <code>{chat_id}</code>\n"
        f"{msg_link_line}\n"
        f"<b>Clone entire chat:</b>\n"
        f"<code>/clone {base_ref}</code>\n"
        f"{topic_section}",
        quote=True,
    )


@Client.on_message(filters.private & filters.command("stats"))
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
