"""
Main message handler with resumable batches.

Every batch gets a unique batch_id saved to MongoDB. Progress is checkpointed
after each message. On /start the bot offers to resume incomplete batches.
"""
import asyncio
import uuid
from time import time as ts

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from config import LOGIN_SYSTEM, MAX_TRANSMISSIONS
from database.db import db
from helpers.msg import parse_range_input, parse_playlist_input, parse_channel_link
from helpers.keyboards import kb_cancel_only
from helpers.downloader import (
    run_batch, register_task, cancel_task, has_running_task,
)
from logger import LOGGER


# ── User client setup ─────────────────────────────────────────────────────────
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
            max_concurrent_transmissions=MAX_TRANSMISSIONS,
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
    dest, label = await db.get_destination(message.from_user.id)
    if dest:
        return dest, label
    return message.chat.id, None


# ── /cancel ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message):
    _WAITING_PLAYLIST.discard(message.from_user.id)
    if cancel_task(message.from_user.id):
        await message.reply("🛑 **Task cancelled.** Batch progress saved — use /resume to continue.")
    else:
        await message.reply("No active task to cancel.")


@Client.on_callback_query(filters.regex("^cancel_task$"))
async def cb_cancel(client: Client, cb: CallbackQuery):
    _WAITING_PLAYLIST.discard(cb.from_user.id)
    if cancel_task(cb.from_user.id):
        await cb.message.edit("🛑 **Task cancelled.** Use /resume to continue.")
    else:
        await cb.answer("No active task.", show_alert=True)


# ── /status ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("status"))
async def cmd_status(client: Client, message: Message):
    if has_running_task(message.from_user.id):
        await message.reply("⚙️ You have an active task. Use /cancel to stop.")
    else:
        await message.reply("✅ No active tasks. Send a link or use /resume for paused batches.")


# ── /resume ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("resume"))
async def cmd_resume(bot: Client, message: Message):
    user_id = message.from_user.id

    if has_running_task(user_id):
        return await message.reply("⚠️ You already have a running task. Use /cancel first.")

    batches = await db.get_active_batches(user_id)
    if not batches:
        return await message.reply("📭 No paused batches to resume.")

    # Build keyboard with one button per resumable batch
    buttons = []
    for b in batches[:5]:
        label = b.get("job_label", "Batch")
        done = b.get("done", 0)
        total = b.get("total", 0)
        pct = (done / total * 100) if total else 0
        btn_text = f"▶️ {label} • {done}/{total} ({pct:.0f}%)"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"resume_{b['batch_id']}")])
    buttons.append([InlineKeyboardButton("🗑 Discard all", callback_data="resume_discard_all")])

    await message.reply(
        "🔁 **Resumable batches found:**\n\n"
        "Pick one to continue, or discard all.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^resume_(.+)$"))
async def cb_resume(bot: Client, cb: CallbackQuery):
    payload = cb.matches[0].group(1)
    user_id = cb.from_user.id

    if payload == "discard_all":
        batches = await db.get_active_batches(user_id)
        for b in batches:
            await db.delete_batch(user_id, b["batch_id"])
        return await cb.message.edit("🗑 All paused batches discarded.")

    batch_id = payload
    batch = await db.get_batch(user_id, batch_id)
    if not batch:
        return await cb.message.edit("❌ Batch no longer exists.")

    if has_running_task(user_id):
        return await cb.answer("You already have a running task.", show_alert=True)

    await cb.message.edit("▶️ Resuming...")
    await _resume_batch(bot, cb.message, batch)


async def _resume_batch(bot: Client, message: Message, batch: dict):
    """Start a saved batch from where it left off."""
    user_id = batch["user_id"]
    batch_id = batch["batch_id"]
    jobs = batch.get("jobs", [])
    current_job_idx = batch.get("current_job_idx", 0)
    current_msg_id = batch.get("current_msg_id", 0)
    target_chat = batch.get("target_chat")
    label_kind = batch.get("label_kind", "Batch")

    acc = await get_acc(bot, message)
    if not acc:
        return

    await db.mark_batch_status(user_id, batch_id, "running")

    async def _run():
        try:
            for i in range(current_job_idx, len(jobs)):
                chat_id, start_id, end_id, topic_id = jobs[i]
                # Only the current job uses resume_from; later jobs start fresh
                resume = current_msg_id if i == current_job_idx else None
                jlabel = f"{label_kind} {i+1}/{len(jobs)}" if len(jobs) > 1 else label_kind

                async def _persist(job_idx, msg_id, done, success, skipped, failed):
                    await db.update_batch_progress(
                        user_id, batch_id, i, msg_id, done, success, skipped, failed,
                    )

                await run_batch(
                    bot, acc, message,
                    chat_id, start_id, end_id,
                    target_chat,
                    topic_id=topic_id,
                    job_label=jlabel,
                    user_id=user_id,
                    batch_id=batch_id,
                    resume_from=resume,
                    job_index=i,
                    total_jobs=len(jobs),
                    progress_callback=_persist,
                )

            await db.mark_batch_status(user_id, batch_id, "done")
            # Delete the completed record after 10 seconds
            await asyncio.sleep(10)
            await db.delete_batch(user_id, batch_id)
        except asyncio.CancelledError:
            await db.mark_batch_status(user_id, batch_id, "paused")
        except Exception as e:
            LOGGER(__name__).error(f"Resume batch error: {e}")
            await db.mark_batch_status(user_id, batch_id, "paused")
        finally:
            await _disconnect_if_needed(acc)

    register_task(user_id, asyncio.create_task(_run()))


# ── /clone ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("clone"))
async def cmd_clone(bot: Client, message: Message):
    if has_running_task(message.from_user.id):
        return await message.reply("⚠️ You already have a running task. Use /cancel first.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply(
            "<b>📋 Clone Channel / Group</b>\n\n"
            "Copies every message from a channel or group to your destination.\n\n"
            "<b>Usage:</b>\n"
            "<code>/clone https://t.me/channelname</code>\n"
            "<code>/clone https://t.me/c/1234567890</code>\n"
            "<code>/clone @channelname</code>\n\n"
            "Set a destination first with /setchannel."
        )

    source = args[1].strip()

    # Parse the channel identifier
    try:
        chat_ref, topic_id = parse_channel_link(source)
    except ValueError as e:
        return await message.reply(f"❌ {e}")

    acc = await get_acc(bot, message)
    if not acc:
        return

    status = await message.reply("🔍 <b>Resolving channel…</b>")

    # Resolve and verify access
    try:
        chat = await acc.get_chat(chat_ref)
    except Exception as e:
        await status.edit(
            f"❌ <b>Could not access the chat.</b>\n"
            f"<i>{e}</i>\n\n"
            "Make sure your account is a member of that channel/group."
        )
        await _disconnect_if_needed(acc)
        return

    chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat.id)

    # Find the latest message ID
    last_id = None
    try:
        async for msg in acc.get_chat_history(chat.id, limit=1):
            last_id = msg.id
    except Exception as e:
        await status.edit(f"❌ Could not fetch message history: <i>{e}</i>")
        await _disconnect_if_needed(acc)
        return

    if not last_id:
        await status.edit("❌ The channel appears to be empty or inaccessible.")
        await _disconnect_if_needed(acc)
        return

    # Find the first real message ID to avoid scanning from 1 needlessly
    first_id = 1
    try:
        async for msg in acc.get_chat_history(chat.id, limit=1, reverse=True):
            first_id = msg.id
    except Exception:
        first_id = 1

    total_slots = last_id - first_id + 1
    target_chat, target_label = await _get_target(message)

    await status.edit(
        f"<b>📋 Clone: {chat_name}</b>\n\n"
        f"📨 Message range: <code>{first_id}</code> → <code>{last_id}</code> "
        f"(~{total_slots} slots)\n"
        f"📤 Destination: <b>{target_label or 'this chat'}</b>\n\n"
        "<b>Starting clone…</b>"
    )

    jobs = [(chat.id, first_id, last_id, topic_id)]
    label = f"Clone: {chat_name[:40]}"

    await _kick_off_jobs(
        bot, message, jobs, label,
        _acc=acc, _target=(target_chat, target_label),
    )


# ── /batch ────────────────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.command("batch"))
async def cmd_batch(bot: Client, message: Message):
    if has_running_task(message.from_user.id):
        return await message.reply("⚠️ You already have a running task. Use /cancel first.")

    args = message.text.split(None, 1)
    if len(args) < 2:
        return await message.reply(
            "**Batch Download**\n\n"
            "Usage: `/batch <start_link> - <end_link>`\n\n"
            "Works with topic links too."
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
    if has_running_task(message.from_user.id):
        return await message.reply("⚠️ You already have a running task. Use /cancel first.")

    _WAITING_PLAYLIST.add(message.from_user.id)
    await message.reply(
        "🎵 **Playlist Mode**\n\n"
        "Send all your links now — one per line.\n"
        "Each line can be a single post, a range (`link1 - link2`), or a topic link.\n\n"
        "Send /cancel to abort."
    )


# ── Generic text handler ──────────────────────────────────────────────────────
@Client.on_message(
    filters.private & filters.text & ~filters.forwarded
    & ~filters.command([
        "start", "help", "login", "logout", "cancel", "resume",
        "status", "batch", "playlist", "clone", "logs", "stats", "broadcast",
        "setchannel", "destination", "resetdest",
    ])
)
async def handle_text(bot: Client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if user_id in _WAITING_PLAYLIST:
        if "t.me" not in text:
            return await message.reply("❌ No valid links. Send links or /cancel.")

        _WAITING_PLAYLIST.discard(user_id)
        jobs, errors = parse_playlist_input(text)
        if errors:
            await message.reply("⚠️ Some lines were skipped:\n" + "\n".join(errors[:5]))
        if not jobs:
            return await message.reply("❌ No valid links. Use /playlist to try again.")
        await _kick_off_jobs(bot, message, jobs, "Playlist")
        return

    if "t.me" not in text:
        return

    try:
        jobs = parse_range_input(text)
    except ValueError as e:
        return await message.reply(f"❌ {e}")

    is_range = (jobs[0][1] != jobs[0][2])
    label_kind = "Batch" if is_range else "Single"
    await _kick_off_jobs(bot, message, jobs, label_kind)


async def _kick_off_jobs(
    bot: Client, message: Message, jobs: list, label_kind: str,
    _acc=None, _target=None,
):
    """Persist batch state and start the task.

    _acc: pre-connected user client (skips get_acc call).
    _target: (target_chat_id, target_label) tuple (skips DB lookup).
    """
    user_id = message.from_user.id

    if has_running_task(user_id):
        return await message.reply("⚠️ Already running. Use /cancel first.")

    acc = _acc or await get_acc(bot, message)
    if not acc:
        return

    target_chat, target_label = _target if _target else await _get_target(message)

    # ── Persist initial batch state ───────────────────────────────────────────
    batch_id = str(uuid.uuid4())[:12]
    total = sum(end - start + 1 for _, start, end, _ in jobs)

    await db.save_batch(user_id, batch_id, {
        "jobs": [list(j) for j in jobs],
        "current_job_idx": 0,
        "current_msg_id": jobs[0][1],
        "target_chat": target_chat,
        "target_label": target_label,
        "label_kind": label_kind,
        "job_label": label_kind,
        "total": total,
        "done": 0,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "status": "running",
    })

    info = []
    info.append(f"📤 Sending to: **{target_label or 'this chat'}**")
    info.append(f"📦 Jobs: **{len(jobs)}** | Total messages: **{total}**")
    info.append(f"🆔 Batch ID: `{batch_id}` (saved — resume with /resume if interrupted)")
    await message.reply("\n".join(info))

    async def _run():
        try:
            for i, (chat_id, start_id, end_id, topic_id) in enumerate(jobs):
                jlabel = f"{label_kind} {i+1}/{len(jobs)}" if len(jobs) > 1 else label_kind

                async def _persist(job_idx, msg_id, done, success, skipped, failed):
                    await db.update_batch_progress(
                        user_id, batch_id, i, msg_id, done, success, skipped, failed,
                    )

                await run_batch(
                    bot, acc, message,
                    chat_id, start_id, end_id,
                    target_chat,
                    topic_id=topic_id,
                    job_label=jlabel,
                    user_id=user_id,
                    batch_id=batch_id,
                    job_index=i,
                    total_jobs=len(jobs),
                    progress_callback=_persist,
                )

            await db.mark_batch_status(user_id, batch_id, "done")
            await asyncio.sleep(10)
            await db.delete_batch(user_id, batch_id)
        except asyncio.CancelledError:
            await db.mark_batch_status(user_id, batch_id, "paused")
        except Exception as e:
            LOGGER(__name__).error(f"Batch error: {e}")
            await db.mark_batch_status(user_id, batch_id, "paused")
        finally:
            await _disconnect_if_needed(acc)

    register_task(user_id, asyncio.create_task(_run()))
