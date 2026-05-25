from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def kb_start_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Single Post", callback_data="mode_single"),
            InlineKeyboardButton("📦 Batch Range", callback_data="mode_batch"),
        ],
        [
            InlineKeyboardButton("🎵 Playlist", callback_data="mode_playlist"),
        ],
    ])


def kb_confirm_cancel(action: str):
    """Generic confirm / cancel pair."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{action}"),
            InlineKeyboardButton("❌ Cancel",  callback_data="cancel_task"),
        ]
    ])


def kb_cancel_only():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_task")]
    ])


def kb_caption(msg_id: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✂️ Trim Last Line", callback_data=f"cap_rmlast_{msg_id}"),
            InlineKeyboardButton("▶️ Start",           callback_data=f"cap_done_{msg_id}"),
        ]
    ])


def kb_login_help():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 How to get API ID & Hash", url="https://youtu.be/LDtgwpI-N7M")]
    ])
