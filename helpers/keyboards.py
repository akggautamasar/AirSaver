from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def kb_cancel_only():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_task")]
    ])


def kb_destination_menu():
    """Choose where to send downloads."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Send to this chat (bot)", callback_data="dest_here")],
        [InlineKeyboardButton("📤 Send to another chat",    callback_data="dest_set")],
        [InlineKeyboardButton("ℹ️ How to set channel/group", callback_data="dest_help")],
    ])


def kb_login_help():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 How to get API ID & Hash", url="https://youtu.be/LDtgwpI-N7M")]
    ])
