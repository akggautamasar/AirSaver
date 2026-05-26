"""
Core download + upload engine with three speed phases:

Phase 1: tuned concurrency (MAX_TRANSMISSIONS=16, MAX_CONCURRENT=6)
Phase 2: in-memory transfer for files under INMEM_THRESHOLD (skip disk)
         parallel file processing in batches (PARALLEL_FILES files at once)
Phase 3: pipelined parallel transfer — start uploading file N
         while file N+1 downloads in background
"""

import os
import io
import asyncio
import json
from time import time as ts

from pyrogram import Client
from pyrogram.types import (
    Message, InputMediaPhoto, InputMediaVideo,
    InputMediaDocument, InputMediaAudio,
)
from pyrogram.errors import (
    FloodWait, FileReferenceExpired, PeerIdInvalid,
    BadRequest, ChatForwardsRestricted,
)

from config import (
    WAITING_TIME, MAX_CONCURRENT, PARALLEL_FILES,
    INMEM_THRESHOLD, PIPELINE_DEPTH,
)
from logger import LOGGER
from helpers.files import (
    get_download_path, get_readable_file_size,
    get_readable_time, cleanup_download, check_file_size,
)
from helpers.msg import get_parsed_msg, clean_caption, apply_caption_rules, get_file_name
from helpers.keyboards import kb_cancel_only


# ── Task registry ─────────────────────────────────────────────────────────────
RUNNING_TASKS: dict = {}
_USER_INFO_CACHE: dict = {}
_CHAT_RESTRICTED_CACHE: dict = {}  # NEW: cache "is this chat restricted?"


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


async def is_chat_restricted(acc: Client, chat_id) -> bool:
    """Cache per (acc, chat_id) — avoid repeated get_chat calls."""
    key = (id(acc), str(chat_id))
    if key in _CHAT_RESTRICTED_CACHE:
        return _CHAT_RESTRICTED_CACHE[key]
    try:
        chat = await acc.get_chat(chat_id)
        restricted = bool(getattr(chat, "has_protected_content", False))
    except Exception:
        restricted = True  # safe default
    _CHAT_RESTRICTED_CACHE[key] = restricted
    return restricted


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


async def get_video_info(path_or_bytes):
    """Accepts a file path OR bytes. Uses stdin for bytes."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        # Write to temp file (ffprobe doesn't like stdin for some containers)
        temp = f"/tmp/probe_{os.getpid()}_{ts()}.bin"
        try:
            with open(temp, "wb") as f:
                f.write(path_or_bytes)
            return await get_video_info(temp)
        finally:
            if os.path.exists(temp):
                os.remove(temp)

    out, _, code = await _run_cmd([
        "ffprobe", "-hide_banner", "-loglevel", "error",
        "-print_format", "json", "-show_format", "-show_streams", path_or_bytes,
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


# ── Progress display ──────────────────────────────────────────────────────────
def _make_bar(pct: float, length: int = 12) -> str:
    filled = int(pct / 100 * length)
    return "█" * filled + "░" * (length - filled)


def format_progress(
    stage: str, filename: str,
    current: int = 0, total: int = 0,
    speed: float = 0, eta: int = 0,
    batch_done: int = 0, batch_total: int = 0,
    job_label: str = "",
    extra: str = "",
) -> str:
    text = ""
    if job_label:
        text += f"<b>📦 {job_label}</b>\n"

    if batch_total > 1:
        batch_pct = (batch_done / batch_total) * 100
        text += (
            f"<b>Overall:</b> {batch_done}/{batch_total} ({batch_pct:.1f}%)\n"
            f"<code>{_make_bar(batch_pct, 12)}</code>\n\n"
        )

    text += f"<b>{stage}</b>\n<code>{filename[:48]}</code>\n"

    if total > 0:
        pct = (current / total) * 100
        text += f"<code>{_make_bar(pct)}</code> {pct:.1f}%\n"
        text += f"<b>{get_readable_file_size(current)}</b> / <b>{get_readable_file_size(total)}</b>"
        if speed > 0:
            text += f"\n⚡ <b>{get_readable_file_size(speed)}/s</b>"
            if eta > 0:
                text += f"  ⏳ ETA: <b>{get_readable_time(eta)}</b>"
    if extra:
        text += f"\n\n<i>{extra}</i>"
    return text


class ProgressTracker:
    """Throttled live progress updater (4-second edit interval)."""
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
        self.extra = ""

    def set_file(self, filename: str, stage: str, extra: str = ""):
        self.current_filename = filename
        self.current_stage = stage
        self.extra = extra
        self.start_time = ts()

    async def update(self, current: int, total: int, force: bool = False):
        now = ts()
        if not force and now - self.last_edit < 4:
            return
        elapsed = max(now - self.start_time, 0.001)
        speed = current / elapsed
        eta = int((total - current) / speed) if speed > 0 and total > current else 0
        text = format_progress(
            self.current_stage, self.current_filename,
            current, total, speed, eta,
            self.batch_done, self.batch_total, self.job_label,
            self.extra,
        )
        if text == self.last_text:
            return
        try:
            await self.status_msg.edit(text, reply_markup=kb_cancel_only())
            self.last_text = text
            self.last_edit = now
        except Exception:
            pass


# ── Server-side copy (instant, unrestricted sources) ──────────────────────────
async def try_copy_message(acc: Client, msg, target_chat_id, topic_id):
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
        LOGGER(__name__).warning(f"copy_message failed for {msg.id}: {e}")
        return False


# ── Send a downloaded file (path OR BytesIO) ─────────────────────────────────
async def send_file(
    bot: Client, chat_id, media_source,
    media_type: str, caption: str,
    topic_id=None, msg_id: int = 0,
    progress_cb=None, file_size: int = 0,
    is_inmem: bool = False, filename: str = "",
) -> bool:
    """
    media_source can be:
      - a file path (str) — when downloaded to disk
      - a BytesIO    — when using in-memory transfer (Phase 2)
    """
    # For in-memory uploads, we need a fake filename
    if is_inmem:
        media_source.name = filename or f"{msg_id}.bin"
        media_source.seek(0)

    kwargs = dict(
        chat_id=chat_id,
        caption=caption or "",
        reply_to_message_id=topic_id,
        progress=progress_cb,
    )
    thumb = None
    try:
        if media_type == "photo":
            kwargs.pop("progress", None)
            await bot.send_photo(photo=media_source, **kwargs)
        elif media_type == "video":
            if is_inmem:
                # Can't probe BytesIO directly — skip metadata, send as video w/ no thumb
                await bot.send_video(
                    video=media_source, supports_streaming=True, **kwargs,
                )
            else:
                dur, w, h = await get_video_info(media_source)
                thumb = await make_thumbnail(media_source, dur, msg_id)
                if w == 0 and h == 0:
                    await bot.send_document(document=media_source, **kwargs)
                else:
                    await bot.send_video(
                        video=media_source, duration=dur, width=w, height=h,
                        thumb=thumb, supports_streaming=True, **kwargs,
                    )
        elif media_type == "audio":
            await bot.send_audio(audio=media_source, **kwargs)
        else:
            await bot.send_document(document=media_source, **kwargs)
        return True
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
        # On retry, reset BytesIO position
        if is_inmem:
            media_source.seek(0)
        return await send_file(
            bot, chat_id, media_source, media_type, caption,
            topic_id, msg_id, progress_cb, file_size, is_inmem, filename,
        )
    except Exception as e:
        LOGGER(__name__).error(f"send_file error: {e}")
        return False
    finally:
        if thumb and os.path.exists(thumb):
            try:
                os.remove(thumb)
            except Exception:
                pass


# ── Phase 2/3: Download a single message into a "DownloadedItem" ─────────────
class DownloadedItem:
    """Holds a downloaded file ready to upload."""
    __slots__ = ("msg", "media_path", "media_buf", "is_inmem",
                 "media_type", "caption", "filename", "msg_id", "file_size")

    def __init__(self, msg, media_type, caption, filename, msg_id, file_size,
                 media_path=None, media_buf=None):
        self.msg = msg
        self.media_path = media_path  # disk path
        self.media_buf = media_buf    # BytesIO (in-memory)
        self.is_inmem = media_buf is not None
        self.media_type = media_type
        self.caption = caption
        self.filename = filename
        self.msg_id = msg_id
        self.file_size = file_size

    def cleanup(self):
        if self.media_path:
            cleanup_download(self.media_path)
        if self.media_buf:
            try:
                self.media_buf.close()
            except Exception:
                pass


async def download_msg(
    acc: Client, msg, caption_rules, user_id: int,
    progress: ProgressTracker = None,
):
    """
    Download a single message, returning a DownloadedItem (or None if skipped).
    Uses in-memory buffer for small files, disk for large ones.
    """
    if not msg or msg.empty:
        return None

    has_media = bool(
        msg.document or msg.video or msg.audio or msg.photo
        or msg.animation or msg.voice or msg.video_note or msg.sticker
    )
    if not has_media:
        # Caller handles text-only messages separately
        return None

    media_obj = (msg.document or msg.video or msg.audio or msg.photo
                 or msg.animation or msg.voice or msg.video_note or msg.sticker)
    file_size = getattr(media_obj, "file_size", 0) or 0
    filename = get_file_name(msg.id, msg)

    caption = await get_parsed_msg(msg)
    caption = clean_caption(caption)
    if caption_rules:
        caption = apply_caption_rules(caption, caption_rules)

    me = await get_cached_me(acc, user_id)
    is_premium = getattr(me, "is_premium", False) if me else False
    if file_size and not await check_file_size(
        file_size, progress.status_msg if progress else None,
        "download", is_premium,
    ):
        return "skip"

    media_type = (
        "photo" if msg.photo else
        "video" if msg.video else
        "audio" if msg.audio else
        "document"
    )

    # Phase 2: in-memory transfer for small files
    use_inmem = file_size > 0 and file_size <= INMEM_THRESHOLD

    if progress:
        method = "RAM" if use_inmem else "disk"
        progress.set_file(filename, "📥 Downloading", extra=f"via {method}")
        await progress.update(0, file_size, force=True)

    async def _dl_progress(current, total):
        if progress:
            await progress.update(current, total)

    for attempt in range(3):
        try:
            if use_inmem:
                buf = await msg.download(in_memory=True, progress=_dl_progress)
                if not buf:
                    return "error"
                return DownloadedItem(
                    msg, media_type, caption, filename, msg.id, file_size,
                    media_buf=buf,
                )
            else:
                path = get_download_path(msg.id, filename)
                path = await msg.download(file_name=path, progress=_dl_progress)
                if not path or not os.path.exists(path):
                    return "error"
                return DownloadedItem(
                    msg, media_type, caption, filename, msg.id, file_size,
                    media_path=path,
                )
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except FileReferenceExpired:
            return "ref_expired"
        except Exception as e:
            if attempt == 2:
                LOGGER(__name__).error(f"Download failed msg {msg.id}: {e}")
                return "error"
            await asyncio.sleep(2)

    return "error"


async def upload_item(
    bot: Client, item: DownloadedItem,
    target_chat_id, topic_id, progress: ProgressTracker = None,
):
    """Upload a previously downloaded item."""
    if progress:
        method = "RAM" if item.is_inmem else "disk"
        progress.set_file(item.filename, "📤 Uploading", extra=f"from {method}")
        await progress.update(0, item.file_size, force=True)

    async def _up_progress(current, total):
        if progress:
            await progress.update(current, total)

    media_source = item.media_buf if item.is_inmem else item.media_path

    ok = await send_file(
        bot, target_chat_id, media_source,
        item.media_type, item.caption, topic_id,
        msg_id=item.msg_id, progress_cb=_up_progress,
        file_size=item.file_size, is_inmem=item.is_inmem,
        filename=item.filename,
    )
    item.cleanup()
    return ok


# ── Text-only / media-group handler (no parallelism needed) ──────────────────
async def process_simple(
    bot: Client, acc: Client, msg,
    target_chat_id, topic_id, caption_rules,
    progress: ProgressTracker = None,
):
    """Handle text-only and media groups (these don't use the pipeline)."""
    caption = await get_parsed_msg(msg)
    caption = clean_caption(caption)
    if caption_rules:
        caption = apply_caption_rules(caption, caption_rules)

    if msg.media_group_id:
        await process_media_group(bot, acc, msg, target_chat_id, topic_id, caption_rules, progress)
        return "ok"

    has_media = bool(
        msg.document or msg.video or msg.audio or msg.photo
        or msg.animation or msg.voice or msg.video_note or msg.sticker
    )
    if not has_media:
        if msg.text or msg.caption:
            await bot.send_message(
                chat_id=target_chat_id,
                message_thread_id=topic_id,
                text=caption or (msg.text.html if msg.text else ""),
                disable_web_page_preview=True,
            )
        return "ok"

    return None  # signal: needs download pipeline


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

    completed = [0]

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

    if media_list:
        try:
            await bot.send_media_group(
                chat_id=target_chat_id, media=media_list,
                reply_to_message_id=topic_id,
            )
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except Exception as e:
            LOGGER(__name__).error(f"Media group send failed: {e}")

    for p in paths:
        cleanup_download(p)


# ── Phase 3: Pipelined batch executor ─────────────────────────────────────────
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
    done = skipped = failed = success = 0
    processed_groups: set = set()

    status_msg = await origin_msg.reply(
        f"<b>📦 {job_label or 'Batch'}</b>\n<b>Starting...</b>\n<code>0/{total}</code>",
        reply_markup=kb_cancel_only(),
    )
    progress = ProgressTracker(status_msg, job_label, 0, total)

    # Try fast path: server-side copy (unrestricted source, no caption rules)
    if not caption_rules:
        if not await is_chat_restricted(acc, chat_id):
            return await _run_batch_fastpath(
                acc, status_msg, progress,
                chat_id, start_id, end_id,
                target_chat_id, topic_id, total, job_label,
            )

    # ── Pipelined download → upload ───────────────────────────────────────────
    # We fetch messages, queue downloads, and start uploads as soon as each finishes.
    # Up to PIPELINE_DEPTH downloads run in parallel.

    upload_queue: asyncio.Queue = asyncio.Queue(maxsize=PIPELINE_DEPTH + 1)
    download_sem = asyncio.Semaphore(PARALLEL_FILES)
    stop_signal = asyncio.Event()

    async def producer():
        """Fetch messages and feed downloads into the pipeline."""
        nonlocal skipped, done
        current = start_id

        while current <= end_id and not stop_signal.is_set():
            chunk_end = min(current + 199, end_id)
            chunk_ids = list(range(current, chunk_end + 1))

            try:
                msgs = await acc.get_messages(chat_id=chat_id, message_ids=chunk_ids)
                if not isinstance(msgs, list):
                    msgs = [msgs]
            except Exception as e:
                LOGGER(__name__).error(f"get_messages failed: {e}")
                current = chunk_end + 1
                continue

            for msg in msgs:
                if stop_signal.is_set():
                    return

                if not msg or msg.empty:
                    await upload_queue.put(("skip", None))
                    continue

                # Skip duplicate media groups
                if msg.media_group_id:
                    if msg.media_group_id in processed_groups:
                        await upload_queue.put(("skip", None))
                        continue
                    processed_groups.add(msg.media_group_id)

                # Simple cases (text, media group) bypass the pipeline
                simple_result = await process_simple(
                    bot, acc, msg, target_chat_id, topic_id, caption_rules, progress,
                )
                if simple_result is not None:
                    await upload_queue.put((simple_result, None))
                    continue

                # Download in background, push result to queue
                async def _dl_and_queue(m):
                    async with download_sem:
                        if stop_signal.is_set():
                            return
                        result = await download_msg(acc, m, caption_rules, user_id, progress)
                        await upload_queue.put((result, m))

                asyncio.create_task(_dl_and_queue(msg))

                await asyncio.sleep(WAITING_TIME)

            current = chunk_end + 1

        # Signal end of work
        await upload_queue.put(("__END__", None))

    async def consumer():
        """Upload completed downloads sequentially in order."""
        nonlocal done, skipped, failed, success
        while True:
            result, msg = await upload_queue.get()

            if result == "__END__":
                # Wait for any still-running downloads
                while not upload_queue.empty():
                    result, msg = await upload_queue.get()
                    await _handle_result(result, msg)
                return

            await _handle_result(result, msg)

    async def _handle_result(result, msg):
        nonlocal done, skipped, failed, success
        try:
            if isinstance(result, DownloadedItem):
                ok = await upload_item(bot, result, target_chat_id, topic_id, progress)
                if ok:
                    success += 1
                else:
                    failed += 1
                done += 1
            elif result == "ok":
                success += 1
                done += 1
            elif result == "skip":
                skipped += 1
                done += 1
            elif result == "ref_expired":
                # Hard to handle gracefully in pipeline — count as failure
                failed += 1
                done += 1
            else:
                failed += 1
                done += 1
            progress.batch_done = done
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER(__name__).error(f"Pipeline upload error: {e}")
            failed += 1
            done += 1

    try:
        await asyncio.gather(producer(), consumer())
    except asyncio.CancelledError:
        stop_signal.set()
        raise

    # Final summary
    try:
        await status_msg.edit(
            f"<blockquote>✅ <b>{job_label or 'Batch'} Complete!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📥 Done: <b>{success}</b>\n"
            f"⏭ Skipped: <b>{skipped}</b>\n"
            f"❌ Failed: <b>{failed}</b>\n"
            f"📊 Total: <b>{done}</b></blockquote>"
        )
    except Exception:
        pass


async def _run_batch_fastpath(
    acc, status_msg, progress,
    chat_id, start_id, end_id, target_chat_id, topic_id,
    total, job_label,
):
    """Fast path: source chat is unrestricted, use server-side copy for all."""
    done = skipped = failed = success = 0
    processed_groups: set = set()
    progress.set_file("Server-side copy", "⚡ Fast forwarding", extra="no transfer needed")

    current = start_id
    while current <= end_id:
        chunk_end = min(current + 199, end_id)
        chunk_ids = list(range(current, chunk_end + 1))

        try:
            msgs = await acc.get_messages(chat_id=chat_id, message_ids=chunk_ids)
            if not isinstance(msgs, list):
                msgs = [msgs]
        except Exception:
            current = chunk_end + 1
            continue

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

            if await try_copy_message(acc, msg, target_chat_id, topic_id):
                success += 1
            else:
                failed += 1
            done += 1

            progress.batch_done = done
            await progress.update(0, 0)
            await asyncio.sleep(0.5)  # lighter throttle for fast path

        current = chunk_end + 1

    try:
        await status_msg.edit(
            f"<blockquote>⚡ <b>{job_label or 'Batch'} Complete (Fast)!</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📥 Done: <b>{success}</b>\n"
            f"⏭ Skipped: <b>{skipped}</b>\n"
            f"❌ Failed: <b>{failed}</b>\n"
            f"📊 Total: <b>{done}</b></blockquote>"
        )
    except Exception:
        pass
