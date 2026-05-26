"""
Main message handler.
Detects links, ranges, /batch, /playlist commands.
Routes downloads to user's configured destination.
"""
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import LOGIN_SYSTEM, MAX_TRANSMISSIONS
from database.db import db
from helpers.msg import parse_range_input, parse_playlist_input
from helpers.keyboards import kb_cancel_only
from helpers.downloader import (
    run_batch, register_task, cancel_task, has_running_task,
)


# ── Utility: get user's Pyrogram client ───────────────────────────────────────
async def get_acc(bot: Client, message: Message):
    if not LOGIN_SYSTEM:
        from bot import TechVJUser
        if TechVJUser is None:
            await message.reply("❌ No shared session configured. Ask the admin.")
            return None
        return TechVJUser

    user_id = message.from_user.id
    session = await db.get_session(user_id)
    if not session:
        await message.reply(
            "⚠️ You need to log in first.\n"
            "Use /login to authenticate with your Telegram account."
        )
        return None

    api_id = await db.get_api_id(user_id)
    api_hash = await db.get_api_hash(user_id)
    try:
        acc = Client(
            ":memory:", session_string=session,
            api_id=api_id, api_hash=api_hash,
            max_concurrent_transmissions=MAX_TRANSMISSIONS,  # SPEED
        )
        await acc.connect()
        return acc
    except Exception:
        await message.reply("❌ Your session has expired. Please /logout and /login again.")
        return None


async def _disconnect_if_needed(acc):
    if LOGIN_SYSTEM:
        try:
            await acc.disconnect()
        except Exception:
            pass


async def _get_target(message: Message):
    """Get user's saved destination, falling back to current chat."""
    dest, label = await db.get_destination(message.from_user.id)
    if dest:
        return dest, label
    return message.chat.id, None


# ── /cancel ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    _WAITING_PLAYLIST.discard(message.from_user.id)
    if cancel_task(message.from_user.id):
        await message.reply("🛑 **Task cancelled.**")
    else:
        await message.reply("No active task to cancel.")


@Client.on_callback_query(filters.regex("^cancel_task$"))
async def cb_cancel(client: Client, cb: CallbackQuery):
    _WAITING_PLAYLIST.discard(cb.from_user.id)
    if cancel_task(cb.from_user.id):
        await cb.message.edit("🛑 **Task cancelled by user.**")
    else:
        await cb.answer("No active task.", show_alert=True)


# ── /status ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("status"))
async def cmd_status(client: Client, message: Message):
    if has_running_task(message.from_user.id):
        await message.reply("⚙️ You have an active task. Use /cancel to stop.")
    else:
        await message.reply("✅ No active tasks. Send a link to get started.")


# ── /batch ────────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("batch"))
async def cmd_batch(bot: Client, message: Message):
    user_id = message.from_user.id
    if has_running_task(user_id):
        return await message.reply("⚠️ You already have a running task. Use /cancel first.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply(
            "**Batch Download**\n\n"
            "Usage: `/batch <start_link> - <end_link>`\n\n"
            "Works with topic links too:\n"
            "`https://t.me/c/123/5/101 - https://t.me/c/123/5/200`"
        )

    try:
        jobs = parse_range_input(args[1])
    except ValueError as e:
        return await message.reply(f"❌ {e}")

    await _kick_off_jobs(bot, message, jobs, "Batch")


# ── /playlist ─────────────────────────────────────────────────────────────────
_WAITING_PLAYLIST: set = set()


@Client.on_message(filters.private & filters.command("playlist"))
async def cmd_playlist(bot: Client, message: Message):
    user_id = message.from_user.id
    if has_running_task(user_id):
        return await message.reply("⚠️ You already have a running task. Use /cancel first.")

    _WAITING_PLAYLIST.add(user_id)
    await message.reply(
        "🎵 **Playlist Mode**\n\n"
        "Send all your links now — one per line.\n"
        "Each line can be:\n"
        "  • A single post link\n"
        "  • A range: `link1 - link2`\n"
        "  • A topic link\n\n"
        "Send /cancel to abort."
    )


# ── Generic text handler ──────────────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text & ~filters.forwarded
    & ~filters.command([
        "start", "help", "login", "logout", "cancel",
        "status", "batch", "playlist", "logs", "stats", "broadcast",
        "setchannel", "destination", "resetdest",
    ])
)
async def handle_text(bot: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # ── Playlist waiting ──────────────────────────────────────────────────────
    if user_id in _WAITING_PLAYLIST:
        if "t.me" not in text:
            return await message.reply(
                "❌ No valid links found.\n"
                "Send links (one per line) or /cancel."
            )

        _WAITING_PLAYLIST.discard(user_id)
        jobs, errors = parse_playlist_input(text)

        if errors:
            await message.reply("⚠️ Some lines were skipped:\n" + "\n".join(errors[:5]))

        if not jobs:
            return await message.reply("❌ No valid links found. Use /playlist to try again.")

        await _kick_off_jobs(bot, message, jobs, "Playlist")
        return

    # ── Regular link(s) ───────────────────────────────────────────────────────
    if "t.me" not in text:
        return

    try:
        jobs = parse_range_input(text)
    except ValueError as e:
        return await message.reply(f"❌ {e}")

    is_range = (jobs[0][1] != jobs[0][2])
    label_kind = "Batch" if is_range else "Single"
    await _kick_off_jobs(bot, message, jobs, label_kind)


async def _kick_off_jobs(bot: Client, message: Message, jobs: list, label_kind: str):
    """Validate, fetch acc, register task."""
    user_id = message.from_user.id

    if has_running_task(user_id):
        return await message.reply(
            "⚠️ You already have a running task. Use /cancel to stop it first."
        )

    acc = await get_acc(bot, message)
    if not acc:
        return

    target_chat, target_label = await _get_target(message)

    # Show destination info
    info_lines = []
    if target_label:
        info_lines.append(f"📤 Sending to: **{target_label}**")
    else:
        info_lines.append("📤 Sending to: **this chat**")
    info_lines.append(f"📦 Jobs queued: **{len(jobs)}**")
    await message.reply("\n".join(info_lines))

    async def _run():
        try:
            for i, (chat_id, start_id, end_id, topic_id) in enumerate(jobs, 1):
                jlabel = f"{label_kind} {i}/{len(jobs)}" if len(jobs) > 1 else label_kind
                await run_batch(
                    bot, acc, message,
                    chat_id, start_id, end_id,
                    target_chat,
                    topic_id=topic_id,
                    job_label=jlabel,
                    user_id=user_id,
                )
        except asyncio.CancelledError:
            pass
        finally:
            await _disconnect_if_needed(acc)

    register_task(user_id, asyncio.create_task(_run()))
