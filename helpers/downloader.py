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
from helpers.floodwait import floodwait_guard, handle_floodwait


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
    for _ in range(10):
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
            await handle_floodwait(e, target_chat_id, floodwait_guard)
        except Exception as e:
            LOGGER(__name__).warning(f"copy_message failed for {msg.id}: {e}")
            return False
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
        for _ in range(10):
            try:
                if media_type == "photo":
                    kw = {k: v for k, v in kwargs.items() if k != "progress"}
                    await bot.send_photo(photo=media_source, **kw)
                elif media_type == "video":
                    if is_inmem:
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
                await handle_floodwait(e, chat_id, floodwait_guard)
                if is_inmem:
                    media_source.seek(0)
                continue
        return False
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

    # 10 min timeout per file; large files (>200 MB on disk) get 30 min
    dl_timeout = 1800 if not use_inmem else 600

    for attempt in range(3):
        try:
            if use_inmem:
                buf = await asyncio.wait_for(
                    msg.download(in_memory=True, progress=_dl_progress),
                    timeout=dl_timeout,
                )
                if not buf:
                    return "error"
                return DownloadedItem(
                    msg, media_type, caption, filename, msg.id, file_size,
                    media_buf=buf,
                )
            else:
                path = get_download_path(msg.id, filename)
                path = await asyncio.wait_for(
                    msg.download(file_name=path, progress=_dl_progress),
                    timeout=dl_timeout,
                )
                if not path or not os.path.exists(path):
                    return "error"
                return DownloadedItem(
                    msg, media_type, caption, filename, msg.id, file_size,
                    media_path=path,
                )
        except asyncio.TimeoutError:
            LOGGER(__name__).warning(f"Download timed out for msg {msg.id}, attempt {attempt+1}")
            if attempt == 2:
                return "error"
            await asyncio.sleep(2)
        except FloodWait as e:
            await handle_floodwait(e, msg.chat.id if msg.chat else None, floodwait_guard)
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
            await handle_floodwait(e, target_chat_id, floodwait_guard)
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
    batch_id: str = None,           # NEW: resumable batch ID
    resume_from: int = None,         # NEW: skip ahead on resume
    job_index: int = 0,              # NEW: which job in playlist
    total_jobs: int = 1,             # NEW: total jobs in playlist
    progress_callback=None,          # NEW: per-message persist callback
):
    total = end_id - start_id + 1
    done = skipped = failed = success = 0
    processed_groups: set = set()

    status_msg = await origin_msg.reply(
        f"<b>📦 {job_label or 'Batch'}</b>\n<b>Starting...</b>\n<code>0/{total}</code>",
        reply_markup=kb_cancel_only(),
    )
    progress = ProgressTracker(status_msg, job_label, 0, total)

    # ── Resume support: skip ahead if requested ──────────────────────────────
    actual_start_id = max(start_id, resume_from) if resume_from else start_id
    if actual_start_id > start_id:
        already_done = actual_start_id - start_id
        done = already_done
        success = already_done
        progress.batch_done = done
        try:
            await status_msg.edit(
                f"<b>📦 {job_label}</b>\n"
                f"<b>Resuming from message {actual_start_id}</b>\n"
                f"<code>{done}/{total}</code>",
                reply_markup=kb_cancel_only(),
            )
        except Exception:
            pass

    # Try fast path: server-side copy (unrestricted source, no caption rules)
    if not caption_rules:
        if not await is_chat_restricted(acc, chat_id):
            return await _run_batch_fastpath(
                acc, status_msg, progress,
                chat_id, actual_start_id, end_id,
                target_chat_id, topic_id, total, job_label,
                user_id=user_id, batch_id=batch_id, progress_callback=progress_callback,
            )

    # ── Ordered pipeline: downloads parallel, uploads strictly in source order ─
    # Each item gets a sequence number. The consumer waits for seq N before
    # uploading, even if N+1, N+2 finished first.

    download_sem = asyncio.Semaphore(PARALLEL_FILES)
    stop_signal = asyncio.Event()
    pending_downloads: list = []           # all spawned download tasks
    results: dict = {}                      # seq_no -> (result, msg)
    results_lock = asyncio.Lock()
    next_seq_ready = asyncio.Event()        # signalled whenever new result lands
    producer_done = asyncio.Event()

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
                failed += 1
                done += 1
            else:
                failed += 1
                done += 1
            progress.batch_done = done
            if progress_callback:
                current_id = (msg.id if msg else (actual_start_id + done - 1))
                try:
                    await progress_callback(
                        job_index, current_id,
                        done, success, skipped, failed,
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOGGER(__name__).error(f"Pipeline upload error: {e}")
            failed += 1
            done += 1

    async def _dl_and_store(seq, m):
        """Download a message and stash the result at its sequence slot."""
        result = "skip"
        try:
            async with download_sem:
                if stop_signal.is_set():
                    async with results_lock:
                        results[seq] = ("skip", m)
                        next_seq_ready.set()
                    return
                result = await download_msg(acc, m, caption_rules, user_id, progress)
        except asyncio.CancelledError:
            # Always store a result so consumer doesn't deadlock
            async with results_lock:
                results[seq] = ("skip", m)
                next_seq_ready.set()
            raise
        except Exception as e:
            LOGGER(__name__).error(f"Download task error msg {m.id}: {e}")
            result = "error"
        async with results_lock:
            results[seq] = (result, m)
            next_seq_ready.set()

    async def producer():
        """Fetch messages and dispatch downloads. Assigns strict sequence numbers."""
        seq = 0
        current = actual_start_id

        while current <= end_id and not stop_signal.is_set():
            chunk_end = min(current + 199, end_id)
            chunk_ids = list(range(current, chunk_end + 1))

            try:
                msgs = await asyncio.wait_for(
                    acc.get_messages(chat_id=chat_id, message_ids=chunk_ids),
                    timeout=90,
                )
                if not isinstance(msgs, list):
                    msgs = [msgs]
            except asyncio.TimeoutError:
                LOGGER(__name__).warning(f"get_messages timed out for chunk {current}-{chunk_end}, retrying")
                await asyncio.sleep(5)
                continue
            except Exception as e:
                LOGGER(__name__).error(f"get_messages failed: {e}")
                current = chunk_end + 1
                continue

            for msg in msgs:
                if stop_signal.is_set():
                    return

                if not msg or msg.empty:
                    async with results_lock:
                        results[seq] = ("skip", None)
                        next_seq_ready.set()
                    seq += 1
                    continue

                if msg.media_group_id:
                    if msg.media_group_id in processed_groups:
                        async with results_lock:
                            results[seq] = ("skip", None)
                            next_seq_ready.set()
                        seq += 1
                        continue
                    processed_groups.add(msg.media_group_id)

                if msg.media_group_id or not bool(
                    msg.document or msg.video or msg.audio or msg.photo
                    or msg.animation or msg.voice or msg.video_note or msg.sticker
                ):
                    # Defer text/group send to the consumer to keep ordering
                    async with results_lock:
                        results[seq] = (("__simple__", msg), msg)
                        next_seq_ready.set()
                    seq += 1
                    continue

                # Throttle producer: count only alive tasks (O(1) with pruning below)
                while sum(1 for t in pending_downloads if not t.done()) >= PARALLEL_FILES + 1:
                    await asyncio.sleep(0.2)

                task = asyncio.create_task(_dl_and_store(seq, msg))
                pending_downloads.append(task)
                seq += 1

                # Prune finished tasks to keep the list small
                if len(pending_downloads) % 50 == 0:
                    pending_downloads[:] = [t for t in pending_downloads if not t.done()]

                if WAITING_TIME > 0:
                    await asyncio.sleep(WAITING_TIME)

            current = chunk_end + 1

        # Wait for all downloads to finish, then signal consumer
        if pending_downloads:
            alive = [t for t in pending_downloads if not t.done()]
            if alive:
                await asyncio.gather(*alive, return_exceptions=True)
        producer_done.set()
        next_seq_ready.set()  # wake the consumer

    async def consumer():
        """Upload completed downloads strictly in seq order."""
        seq = 0
        while True:
            # Wait until results[seq] is available OR producer is done with no more coming
            while True:
                async with results_lock:
                    if seq in results:
                        result, msg = results.pop(seq)
                        break
                    if producer_done.is_set() and not pending_downloads_alive():
                        return
                    next_seq_ready.clear()
                # Timeout prevents a permanent deadlock if an event is ever missed
                try:
                    await asyncio.wait_for(next_seq_ready.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

            # Handle deferred simple message (text/group) here, in order
            if isinstance(result, tuple) and len(result) == 2 and result[0] == "__simple__":
                simple_msg = result[1]
                simple_result = await process_simple(
                    bot, acc, simple_msg, target_chat_id, topic_id, caption_rules, progress,
                )
                await _handle_result(simple_result or "ok", simple_msg)
            else:
                await _handle_result(result, msg)
            seq += 1

    def pending_downloads_alive():
        return any(not t.done() for t in pending_downloads)

    try:
        await asyncio.gather(producer(), consumer())
    except asyncio.CancelledError:
        stop_signal.set()
        for t in pending_downloads:
            if not t.done():
                t.cancel()
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
    user_id: int = 0, batch_id: str = None, progress_callback=None,
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
            msgs = await asyncio.wait_for(
                acc.get_messages(chat_id=chat_id, message_ids=chunk_ids),
                timeout=90,
            )
            if not isinstance(msgs, list):
                msgs = [msgs]
        except asyncio.TimeoutError:
            LOGGER(__name__).warning(f"get_messages timed out (fast path) chunk {current}-{chunk_end}, retrying")
            await asyncio.sleep(5)
            continue
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
            if progress_callback:
                try:
                    await progress_callback(0, msg.id, done, success, skipped, failed)
                except Exception:
                    pass

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
