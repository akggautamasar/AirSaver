"""
Main message handler.
Detects links, ranges, /batch, /playlist commands.
Each user's job runs in its own asyncio.Task so users don't block each other.
"""
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery

from config import LOGIN_SYSTEM
from database.db import db
from helpers.msg import parse_range_input, parse_playlist_input
from helpers.keyboards import kb_cancel_only  # kb_start_menu removed (unused)
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
        acc = Client(":memory:", session_string=session,
                     api_id=api_id, api_hash=api_hash)
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


# ── /cancel ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    # Also clear playlist waiting state
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
        await message.reply("⚙️ You have an active task running. Use /cancel to stop it.")
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

    acc = await get_acc(bot, message)
    if not acc:
        return

    async def _run():
        try:
            for i, (chat_id, start_id, end_id, topic_id) in enumerate(jobs, 1):
                label = f"Batch {i}/{len(jobs)}" if len(jobs) > 1 else "Batch"
                await run_batch(bot, acc, message, chat_id, start_id, end_id,
                                message.chat.id, topic_id=topic_id, job_label=label)
        except asyncio.CancelledError:
            pass
        finally:
            await _disconnect_if_needed(acc)

    register_task(user_id, asyncio.create_task(_run()))


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
        "Example:\n"
        "<code>https://t.me/channel/101\n"
        "https://t.me/c/123456/5/200 - https://t.me/c/123456/5/250\n"
        "https://t.me/group/10/305</code>\n\n"
        "Send all links in a **single message** when ready.\n"
        "Send /cancel to abort."
    )


# ── Generic text handler ──────────────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text
    & ~filters.command([
        "start", "help", "login", "logout", "cancel",
        "status", "batch", "playlist", "logs", "stats", "broadcast"
    ])
)
async def handle_text(bot: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    # ── Playlist links received ────────────────────────────────────────────────
    if user_id in _WAITING_PLAYLIST:
        # fix: ignore non-link messages while waiting (e.g. stray text)
        if "t.me" not in text:
            return await message.reply(
                "❌ No valid links found in that message.\n"
                "Send your links (one per line) or /cancel to abort."
            )

        _WAITING_PLAYLIST.discard(user_id)
        jobs, errors = parse_playlist_input(text)

        if errors:
            await message.reply("⚠️ Some lines were skipped:\n" + "\n".join(errors[:5]))

        if not jobs:
            return await message.reply("❌ No valid links found. Use /playlist to try again.")

        if has_running_task(user_id):
            return await message.reply("⚠️ You already have a running task. Use /cancel first.")

        acc = await get_acc(bot, message)
        if not acc:
            return

        total_jobs = len(jobs)
        confirm_msg = await message.reply(
            f"🎵 **Playlist ready: {total_jobs} job(s)**\n\nStarting now...",
            reply_markup=kb_cancel_only(),
        )

        async def _run_playlist():
            try:
                for i, (chat_id, start_id, end_id, topic_id) in enumerate(jobs, 1):
                    await run_batch(
                        bot, acc, message, chat_id, start_id, end_id,
                        message.chat.id, topic_id=topic_id,
                        job_label=f"Playlist {i}/{total_jobs}",
                    )
            except asyncio.CancelledError:
                try:
                    await confirm_msg.edit("🛑 Playlist cancelled.")
                except Exception:
                    pass
            finally:
                await _disconnect_if_needed(acc)

        register_task(user_id, asyncio.create_task(_run_playlist()))
        return

    # ── Regular link ──────────────────────────────────────────────────────────
    if "t.me" not in text:
        return

    if has_running_task(user_id):
        return await message.reply(
            "⚠️ You already have a running task. Use /cancel to stop it first."
        )

    try:
        jobs = parse_range_input(text)
    except ValueError as e:
        return await message.reply(f"❌ {e}")

    is_range = (jobs[0][1] != jobs[0][2])
    total = sum(end - start + 1 for _, start, end, _ in jobs)
    label = f"Batch ({total} posts)" if is_range else "Single post"

    acc = await get_acc(bot, message)
    if not acc:
        return

    async def _run_jobs():
        try:
            for i, (chat_id, start_id, end_id, topic_id) in enumerate(jobs, 1):
                jlabel = f"{label} {i}/{len(jobs)}" if len(jobs) > 1 else label
                await run_batch(bot, acc, message, chat_id, start_id, end_id,
                                message.chat.id, topic_id=topic_id, job_label=jlabel)
        except asyncio.CancelledError:
            pass
        finally:
            await _disconnect_if_needed(acc)

    register_task(user_id, asyncio.create_task(_run_jobs()))
