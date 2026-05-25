"""
Core download + upload logic.
Handles single messages, media groups, and the per-job batch loop.
Each user's task runs in its own asyncio.Task so users don't block each other.
"""

import os
import asyncio
import json
from time import time

from pyrogram import Client
from pyrogram.types import Message, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from pyrogram.errors import FloodWait, FileReferenceExpired, PeerIdInvalid, BadRequest

from config import WAITING_TIME, MAX_CONCURRENT
from logger import LOGGER
from helpers.files import (
    get_download_path, get_readable_file_size,
    get_readable_time, cleanup_download, check_file_size,
)
from helpers.msg import get_parsed_msg, clean_caption, apply_caption_rules, get_file_name
from helpers.keyboards import kb_cancel_only  # fix: proper import not lazy __import__

# ── Running tasks registry (per user_id) ──────────────────────────────────────
RUNNING_TASKS: dict[int, asyncio.Task] = {}

def register_task(user_id: int, task: asyncio.Task):
    RUNNING_TASKS[user_id] = task
    def _cleanup(_):
        RUNNING_TASKS.pop(user_id, None)
    task.add_done_callback(_cleanup)

def cancel_task(user_id: int) -> bool:
    task = RUNNING_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()
        return True
    return False

def has_running_task(user_id: int) -> bool:
    task = RUNNING_TASKS.get(user_id)
    return task is not None and not task.done()


# ── ffmpeg helpers ────────────────────────────────────────────────────────────
async def _run_cmd(cmd: list) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return "", "timeout", 1
    return stdout.decode().strip(), stderr.decode().strip(), proc.returncode


async def get_video_info(path: str):
    try:
        out, _, code = await _run_cmd([
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-print_format", "json", "-show_format", "-show_streams", path,
        ])
        if code != 0:
            return 0, 640, 480
        data = json.loads(out)
        duration = int(float(data.get("format", {}).get("duration", 0)))
        w, h = 640, 480
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width", 640), s.get("height", 480)
                break
        return duration, w, h
    except FileNotFoundError:
        # ffprobe not installed — return safe defaults
        return 0, 0, 0
    except Exception:
        return 0, 640, 480


async def make_thumbnail(path: str, duration: int, msg_id: int):
    try:
        os.makedirs("thumbs", exist_ok=True)
        out = f"thumbs/thumb_{msg_id}.jpg"
        seek = max(duration // 2, 1)
        _, _, code = await _run_cmd([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(seek), "-i", path,
            "-vframes", "1", "-q:v", "2", "-y", out,
        ])
        return out if code == 0 and os.path.exists(out) else None
    except FileNotFoundError:
        # ffmpeg not installed
        return None
    except Exception:
        return None


# ── Progress text ─────────────────────────────────────────────────────────────
def progress_text(filename: str, done: int, total: int, job_label: str = "") -> str:
    pct = (done / total * 100) if total else 0
    bar_filled = int(pct / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    return (
        f"<b>{'📥 ' + job_label if job_label else '📥 Processing'}</b>\n\n"
        f"<code>{filename[:50]}</code>\n\n"
        f"[{bar}] {pct:.1f}%\n"
        f"<b>{done}</b> / <b>{total}</b> done"
    )


# ── Send a single downloaded file ─────────────────────────────────────────────
async def send_file(
    bot: Client, chat_id: int, media_path: str,
    media_type: str, caption: str,
    topic_id=None, reply_markup=None, msg_id: int = 0,
) -> bool:
    kwargs = dict(
        chat_id=chat_id,
        caption=caption or "",
        reply_to_message_id=topic_id,
        reply_markup=reply_markup,
    )
    thumb = None
    try:
        if media_type == "photo":
            await bot.send_photo(photo=media_path, **kwargs)
        elif media_type == "video":
            dur, w, h = await get_video_info(media_path)
            thumb = await make_thumbnail(media_path, dur, msg_id)
            if w == 0 and h == 0:
                # ffprobe not available — send as document to avoid crash
                await bot.send_document(document=media_path, **kwargs)
            else:
                await bot.send_video(
                    video=media_path, duration=dur, width=w, height=h,
                    thumb=thumb, supports_streaming=True, **kwargs,
                )
        elif media_type == "audio":
            await bot.send_audio(audio=media_path, **kwargs)
        else:
            await bot.send_document(document=media_path, **kwargs)
        return True
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
        return await send_file(bot, chat_id, media_path, media_type, caption,
                               topic_id, reply_markup, msg_id)
    except Exception as e:
        LOGGER(__name__).error(f"send_file error: {e}")
        return False
    finally:
        if thumb and os.path.exists(thumb):
            try:
                os.remove(thumb)
            except Exception:
                pass


# ── Download + send one message ───────────────────────────────────────────────
async def process_one(
    bot: Client, acc: Client,
    chat_id, msg_id: int,
    target_chat_id: int,
    topic_id=None,
    caption_rules=None,
    status_msg: Message = None,
    job_label: str = "",
):
    media_path = None
    try:
        msg = await acc.get_messages(chat_id=chat_id, message_ids=msg_id)
        if not msg or msg.empty:
            return "skip"

        caption = await get_parsed_msg(msg)
        caption = clean_caption(caption)
        if caption_rules:
            caption = apply_caption_rules(caption, caption_rules)

        has_media = bool(
            msg.document or msg.video or msg.audio or msg.photo
            or msg.animation or msg.voice or msg.video_note or msg.sticker
        )

        if msg.media_group_id:
            await process_media_group(bot, acc, msg, target_chat_id, topic_id, caption_rules)
            return "ok"

        if not has_media:
            if msg.text or msg.caption:
                await bot.send_message(
                    chat_id=target_chat_id,
                    message_thread_id=topic_id,
                    text=caption or (msg.text.html if msg.text else ""),
                    disable_web_page_preview=True,
                )
            return "ok"

        # Check file size — fix: use status_msg only if it's a real Message
        media_obj = (msg.document or msg.video or msg.audio or msg.photo
                     or msg.animation or msg.voice or msg.video_note or msg.sticker)
        file_size = getattr(media_obj, "file_size", 0) or 0

        # fix: get is_premium properly via get_me() since acc.me may be None
        try:
            me = await acc.get_me()
            is_premium = getattr(me, "is_premium", False)
        except Exception:
            is_premium = False

        if file_size and not await check_file_size(file_size, status_msg, "download", is_premium):
            return "skip"

        filename = get_file_name(msg_id, msg)
        media_path = get_download_path(msg_id, filename)

        for attempt in range(3):
            try:
                media_path = await msg.download(file_name=media_path)
                break
            except FloodWait as e:
                await asyncio.sleep(int(e.value) + 1)
            except FileReferenceExpired:
                raise
            except Exception as e:
                if attempt == 2:
                    LOGGER(__name__).error(f"Download failed msg {msg_id}: {e}")
                    return "error"
                await asyncio.sleep(2)

        if not media_path or not os.path.exists(media_path):
            return "error"

        media_type = (
            "photo" if msg.photo else
            "video" if msg.video else
            "audio" if msg.audio else
            "document"
        )

        ok = await send_file(
            bot, target_chat_id, media_path, media_type,
            caption, topic_id, msg_id=msg_id,
        )
        return "ok" if ok else "error"

    except FileReferenceExpired:
        return "ref_expired"
    except (PeerIdInvalid, BadRequest) as e:
        LOGGER(__name__).warning(f"Access error msg {msg_id}: {e}")
        return "error"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        LOGGER(__name__).error(f"process_one error msg {msg_id}: {e}")
        return "error"
    finally:
        if media_path:
            cleanup_download(media_path)


# ── Media group ───────────────────────────────────────────────────────────────
async def process_media_group(
    bot: Client, acc: Client, trigger_msg,
    target_chat_id: int, topic_id,
    caption_rules,
):
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        group_msgs = await trigger_msg.get_media_group()
    except Exception:
        return

    async def _dl_one(m):
        filename = get_file_name(m.id, m)
        path = get_download_path(m.id, filename)
        async with sem:
            try:
                path = await m.download(file_name=path)
            except Exception:
                return None, None
        cap = await get_parsed_msg(m)
        cap = clean_caption(cap)
        if caption_rules:
            cap = apply_caption_rules(cap, caption_rules)
        if m.photo:
            return path, InputMediaPhoto(media=path, caption=cap)
        elif m.video:
            return path, InputMediaVideo(media=path, caption=cap)
        elif m.document:
            return path, InputMediaDocument(media=path, caption=cap)
        elif m.audio:
            return path, InputMediaAudio(media=path, caption=cap)
        return path, None

    results = await asyncio.gather(*[_dl_one(m) for m in group_msgs])
    paths, media_list = [], []
    for path, media in results:
        if path:
            paths.append(path)
        if media:
            media_list.append(media)

    if media_list:
        try:
            await bot.send_media_group(
                chat_id=target_chat_id,
                media=media_list,
                reply_to_message_id=topic_id,
            )
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except Exception as e:
            LOGGER(__name__).error(f"Media group send failed: {e}")

    for p in paths:
        cleanup_download(p)


# ── Batch job (range of message IDs) ─────────────────────────────────────────
async def run_batch(
    bot: Client, acc: Client,
    origin_msg: Message,
    chat_id, start_id: int, end_id: int,
    target_chat_id: int,
    topic_id=None,
    caption_rules=None,
    job_label: str = "",
):
    total = end_id - start_id + 1
    done = skipped = failed = 0
    processed_groups: set = set()

    status_msg = await origin_msg.reply(
        progress_text("Starting...", 0, total, job_label),
        reply_markup=kb_cancel_only(),
    )

    current = start_id
    last_edit = time()

    while current <= end_id:
        chunk_end = min(current + 199, end_id)
        chunk_ids = list(range(current, chunk_end + 1))

        try:
            msgs = await acc.get_messages(chat_id=chat_id, message_ids=chunk_ids)
            if not isinstance(msgs, list):
                msgs = [msgs]
        except Exception as e:
            LOGGER(__name__).error(f"get_messages failed: {e}")
            failed += len(chunk_ids)
            done += len(chunk_ids)
            current = chunk_end + 1
            continue

        ref_expired_at = None

        for msg in msgs:
            if not msg or msg.empty:
                skipped += 1
                done += 1
                continue

            if msg.media_group_id:
                if msg.media_group_id in processed_groups:
                    skipped += 1
                    done += 1
                    continue
                processed_groups.add(msg.media_group_id)

            result = await process_one(
                bot, acc, chat_id, msg.id,
                target_chat_id, topic_id, caption_rules,
                status_msg, job_label,
            )

            if result == "ok":
                done += 1
            elif result == "skip":
                skipped += 1
                done += 1
            elif result == "ref_expired":
                ref_expired_at = msg.id
                break
            else:
                failed += 1
                done += 1

            # Update progress every 3 seconds
            if time() - last_edit > 3:
                try:
                    fname = get_file_name(msg.id, msg) if msg else "..."
                    await status_msg.edit(
                        progress_text(fname, done, total, job_label),
                        reply_markup=kb_cancel_only(),
                    )
                    last_edit = time()
                except Exception:
                    pass

            await asyncio.sleep(WAITING_TIME)

        if ref_expired_at is not None:
            current = ref_expired_at
            await asyncio.sleep(2)
            continue

        current = chunk_end + 1

    # Final summary
    try:
        await status_msg.edit(
            f"<blockquote>✅ <b>{job_label or 'Batch'} Complete!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📥 Done: <b>{done}</b>\n"
            f"⏭ Skipped: <b>{skipped}</b>\n"
            f"❌ Failed: <b>{failed}</b></blockquote>"
        )
    except Exception:
        pass
