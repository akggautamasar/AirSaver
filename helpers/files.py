import os
from logger import LOGGER

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def get_readable_file_size(size: float) -> str:
    if not size or size < 0:
        return "0 B"
    for unit in SIZE_UNITS:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return "Too large"


def get_readable_time(seconds: int) -> str:
    result = ""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:    result += f"{days}d "
    if hours:   result += f"{hours}h "
    if minutes: result += f"{minutes}m "
    result += f"{secs}s"
    return result.strip()


def get_download_path(message_id: int, filename: str, root: str = "downloads") -> str:
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, filename)


def cleanup_download(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        LOGGER(__name__).warning(f"Cleanup failed for {path}: {e}")


async def check_file_size(file_size: int, message, action: str = "download", is_premium: bool = False) -> bool:
    limit = 4 * 1024 * 1024 * 1024 if is_premium else 2 * 1024 * 1024 * 1024
    if file_size and file_size > limit:
        msg = (
            f"❌ File size **{get_readable_file_size(file_size)}** exceeds "
            f"the **{get_readable_file_size(limit)}** {action} limit."
        )
        # only reply if message is a real Message object with .reply()
        if message and hasattr(message, "reply"):
            await message.reply(msg)
        return False
    return True
