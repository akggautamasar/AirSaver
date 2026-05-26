"""
Core download + upload engine.

Speed optimizations applied:
  • copy_message used for unrestricted source chats (server-side, no transfer)
  • get_me() result cached
  • Concurrent download via Pyrogram chunks (MAX_TRANSMISSIONS)
  • Real-time progress callback during download AND upload
  • Minimal sleep between operations
"""

import os
import asyncio
import json
import time
from time import time as ts

from pyrogram import Client
from pyrogram.types import Message, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from pyrogram.errors import FloodWait, FileReferenceExpired, PeerIdInvalid, BadRequest, ChatForwardsRestricted

from config import WAITING_TIME, MAX_CONCURRENT
from logger import LOGGER
from helpers.files import (
    get_download_path, get_readable_file_size,
    get_readable_time, cleanup_download, check_file_size,
)
from helpers.msg import get_parsed_msg, clean_caption, apply_caption_rules, get_file_name
from helpers.keyboards import kb_cancel_only


# ── Running tasks registry ────────────────────────────────────────────────────
RUNNING_TASKS: dict = {}

# Cache for user info (avoid repeated get_me calls)
_USER_INFO_CACHE: dict = {}


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


async def get_cached_me(acc: Client, user_id: int):
    if user_id not in _USER_INFO_CACHE:
        try:
            _USER_INFO_CACHE[user_id] = await acc.get_me()
        except Exception:
            _USER_INFO_CACHE[user_id] = None
    return _USER_INFO_CACHE[user_id]


# ── ffmpeg helpers ────────────────────────────────────────────────────────────
async def _run_cmd(cmd: list):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        return stdout.decode().strip(), stderr.decode().strip(), proc.returncode
    except (FileNotFoundError, asyncio.TimeoutError):
        return "", "missing or timeout", 1
    except Exception:
        return "", "error", 1


async def get_video_info(path: str):
    out, _, code = await _run_cmd([
        "ffprobe", "-hide_banner", "-loglevel", "error",
        "-print_format", "json", "-show_format", "-show_streams", path,
    ])
    if code != 0 or not out:
        return 0, 0, 0
    try:
        data = json.loads(out)
        duration = int(float(data.get("format", {}).get("duration", 0)))
        w, h = 640, 480
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                w, h = s.get("width", 640), s.get("height", 480)
                break
        return duration, w, h
    except Exception:
        return 0, 0, 0


async def make_thumbnail(path: str, duration: int, msg_id: int):
    os.makedirs("thumbs", exist_ok=True)
    out = f"thumbs/thumb_{msg_id}.jpg"
    seek = max(duration // 2, 1)
    _, _, code = await _run_cmd([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", str(seek), "-i", path,
        "-vframes", "1", "-q:v", "2", "-y", out,
    ])
    return out if code == 0 and os.path.exists(out) else None


# ── Pretty progress bar ───────────────────────────────────────────────────────
def _make_bar(pct: float, length: int = 12) -> str:
    filled = int(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


def format_progress(
    stage: str,
    filename: str,
    current: int = 0,
    total: int = 0,
    speed: float = 0,
    eta: int = 0,
    batch_done: int = 0,
    batch_total: int = 0,
    job_label: str = "",
) -> str:
    """Build a rich progress message."""
    text = ""

    if job_label:
        text += f"<b>📦 {job_label}</b>\n"

    if batch_total > 1:
        batch_pct = (batch_done / batch_total) * 100
        text += (
            f"<b>Overall:</b> {batch_done}/{batch_total} "
            f"({batch_pct:.1f}%)\n"
            f"<code>{_make_bar(batch_pct, 12)}</code>\n\n"
        )

    text += f"<b>{stage}</b>\n<code>{filename[:48]}</code>\n"

    if total > 0:
        pct = (current / total) * 100
        text += f"<code>{_make_bar(pct)}</code> {pct:.1f}%\n"
        text += f"<b>{get_readable_file_size(current)}</b> / "
        text += f"<b>{get_readable_file_size(total)}</b>"
        if speed > 0:
            text += f"\n⚡ <b>{get_readable_file_size(speed)}/s</b>"
            if eta > 0:
                text += f"  ⏳ ETA: <b>{get_readable_time(eta)}</b>"

    return text


# ── Live progress updater (rate-limited) ──────────────────────────────────────
class ProgressTracker:
    """Tracks transfer progress; throttles message edits to avoid flood."""
    def __init__(self, status_msg: Message, job_label: str,
                 batch_done: int, batch_total: int):
        self.status_msg = status_msg
        self.job_label = job_label
        self.batch_done = batch_done
        self.batch_total = batch_total
        self.start_time = ts()
        self.last_edit = 0.0
        self.last_text = ""
        self.current_filename = ""
        self.current_stage = "📥 Downloading"

    def set_file(self, filename: str, stage: str):
        self.current_filename = filename
        self.current_stage = stage
        self.start_time = ts()

    async def update(self, current: int, total: int, force: bool = False):
        now = ts()
        # Throttle: edit at most every 4 seconds
        if not force and now - self.last_edit < 4:
            return

        elapsed = max(now - self.start_time, 0.001)
        speed = current / elapsed
        eta = int((total - current) / speed) if speed > 0 and total > current else 0

        text = format_progress(
            self.current_stage, self.current_filename,
            current, total, speed, eta,
            self.batch_done, self.batch_total, self.job_label,
        )
        if text == self.last_text:
            return

        try:
            await self.status_msg.edit(text, reply_markup=kb_cancel_only())
            self.last_text = text
            self.last_edit = now
        except Exception:
            pass


# ── Send a downloaded file ────────────────────────────────────────────────────
async def send_file(
    bot: Client, chat_id, media_path: str,
    media_type: str, caption: str,
    topic_id=None, msg_id: int = 0,
    progress_cb=None,
) -> bool:
    kwargs = dict(
        chat_id=chat_id,
        caption=caption or "",
        reply_to_message_id=topic_id,
        progress=progress_cb,
    )
    thumb = None
    try:
        if media_type == "photo":
            kwargs.pop("progress", None)  # photos don't show progress
            await bot.send_photo(photo=media_path, **kwargs)
        elif media_type == "video":
            dur, w, h = await get_video_info(media_path)
            thumb = await make_thumbnail(media_path, dur, msg_id)
            if w == 0 and h == 0:
                # ffprobe missing → send as document
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
        return await send_file(bot, chat_id, media_path, media_type,
                               caption, topic_id, msg_id, progress_cb)
    except Exception as e:
        LOGGER(__name__).error(f"send_file error: {e}")
        return False
    finally:
        if thumb and os.path.exists(thumb):
            try:
                os.remove(thumb)
            except Exception:
                pass


# ── Try fast copy first (unrestricted source) ─────────────────────────────────
async def try_copy_message(
    acc: Client, msg, target_chat_id, topic_id,
):
    """
    Attempt server-side copy (no download/upload).
    Returns True if successful, False if source is restricted.
    """
    try:
        await acc.copy_message(
            chat_id=target_chat_id,
            from_chat_id=msg.chat.id,
            message_id=msg.id,
            message_thread_id=topic_id,
        )
        return True
    except ChatForwardsRestricted:
        return False
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
        return await try_copy_message(acc, msg, target_chat_id, topic_id)
    except Exception as e:
        # Some other error — fall back to download
        LOGGER(__name__).warning(f"copy_message failed for {msg.id}: {e}")
        return False


# ── Download + send one message ───────────────────────────────────────────────
async def process_one(
    bot: Client, acc: Client,
    chat_id, msg_id: int,
    target_chat_id,
    topic_id=None,
    caption_rules=None,
    progress: ProgressTracker = None,
    user_id: int = 0,
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

        # ── Try FAST PATH: server-side copy if source is unrestricted ─────────
        if not caption_rules:  # caption rules require download
            try:
                src_chat = await acc.get_chat(chat_id)
                if not getattr(src_chat, "has_protected_content", False):
                    if progress:
                        progress.set_file(
                            get_file_name(msg_id, msg) if not msg.empty else f"msg_{msg_id}",
                            "⚡ Fast copy",
                        )
                    if await try_copy_message(acc, msg, target_chat_id, topic_id):
                        return "ok"
            except Exception:
                pass  # fall through to download path

        # ── Media group ───────────────────────────────────────────────────────
        if msg.media_group_id:
            await process_media_group(bot, acc, msg, target_chat_id, topic_id, caption_rules, progress)
            return "ok"

        has_media = bool(
            msg.document or msg.video or msg.audio or msg.photo
            or msg.animation or msg.voice or msg.video_note or msg.sticker
        )

        # ── Text only ─────────────────────────────────────────────────────────
        if not has_media:
            if msg.text or msg.caption:
                await bot.send_message(
                    chat_id=target_chat_id,
                    message_thread_id=topic_id,
                    text=caption or (msg.text.html if msg.text else ""),
                    disable_web_page_preview=True,
                )
            return "ok"

        # ── Get file metadata ─────────────────────────────────────────────────
        media_obj = (msg.document or msg.video or msg.audio or msg.photo
                     or msg.animation or msg.voice or msg.video_note or msg.sticker)
        file_size = getattr(media_obj, "file_size", 0) or 0
        filename = get_file_name(msg_id, msg)

        if progress:
            progress.set_file(filename, "📥 Downloading")
            await progress.update(0, file_size, force=True)

        me = await get_cached_me(acc, user_id)
        is_premium = getattr(me, "is_premium", False) if me else False

        if file_size and not await check_file_size(
            file_size, progress.status_msg if progress else None,
            "download", is_premium
        ):
            return "skip"

        media_path = get_download_path(msg_id, filename)

        # ── Download with progress ────────────────────────────────────────────
        async def _dl_progress(current, total):
            if progress:
                await progress.update(current, total)

        for attempt in range(3):
            try:
                media_path = await msg.download(
                    file_name=media_path,
                    progress=_dl_progress,
                )
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

        # ── Upload with progress ──────────────────────────────────────────────
        if progress:
            progress.set_file(filename, "📤 Uploading")
            await progress.update(0, file_size, force=True)

        media_type = (
            "photo" if msg.photo else
            "video" if msg.video else
            "audio" if msg.audio else
            "document"
        )

        async def _up_progress(current, total):
            if progress:
                await progress.update(current, total)

        ok = await send_file(
            bot, target_chat_id, media_path, media_type,
            caption, topic_id, msg_id=msg_id,
            progress_cb=_up_progress,
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
    target_chat_id, topic_id, caption_rules,
    progress: ProgressTracker = None,
):
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    try:
        group_msgs = await trigger_msg.get_media_group()
    except Exception:
        return

    if progress:
        progress.set_file(f"Media group ({len(group_msgs)} items)", "📥 Downloading")
        await progress.update(0, len(group_msgs), force=True)

    completed = [0]  # mutable for closure

    async def _dl_one(m):
        filename = get_file_name(m.id, m)
        path = get_download_path(m.id, filename)
        async with sem:
            try:
                path = await m.download(file_name=path)
            except Exception:
                return None, None
        completed[0] += 1
        if progress:
            await progress.update(completed[0], len(group_msgs))

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

    if progress:
        progress.set_file(f"Media group ({len(media_list)} items)", "📤 Uploading")
        await progress.update(0, len(media_list), force=True)

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


# ── Batch loop ────────────────────────────────────────────────────────────────
async def run_batch(
    bot: Client, acc: Client,
    origin_msg: Message,
    chat_id, start_id: int, end_id: int,
    target_chat_id,
    topic_id=None,
    caption_rules=None,
    job_label: str = "",
    user_id: int = 0,
):
    total = end_id - start_id + 1
    done = skipped = failed = 0
    processed_groups: set = set()

    status_msg = await origin_msg.reply(
        f"<b>📦 {job_label or 'Batch'}</b>\n"
        f"<b>Starting...</b>\n"
        f"<code>0/{total}</code>",
        reply_markup=kb_cancel_only(),
    )

    progress = ProgressTracker(status_msg, job_label, 0, total)

    current = start_id

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
                progress.batch_done = done
                continue

            if msg.media_group_id:
                if msg.media_group_id in processed_groups:
                    skipped += 1
                    done += 1
                    progress.batch_done = done
                    continue
                processed_groups.add(msg.media_group_id)

            result = await process_one(
                bot, acc, chat_id, msg.id,
                target_chat_id, topic_id, caption_rules,
                progress, user_id,
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

            progress.batch_done = done
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
            f"📥 Done: <b>{done - skipped - failed}</b>\n"
            f"⏭ Skipped: <b>{skipped}</b>\n"
            f"❌ Failed: <b>{failed}</b>\n"
            f"📊 Total processed: <b>{done}</b></blockquote>"
        )
    except Exception:
        pass
