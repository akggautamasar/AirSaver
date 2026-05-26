"""
Manage user's download destination (where bot sends files).

Commands:
  /setchannel   - interactive setup
  /destination  - show current destination
  /resetdest    - reset to private chat

User can also forward any message FROM the target channel to set it.
"""
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery
from pyrogram.errors import ChannelInvalid, PeerIdInvalid, BadRequest

from database.db import db
from helpers.keyboards import kb_destination_menu

# Track which users are in "waiting for forward" state
_WAITING_DEST_FORWARD: set = set()


@Client.on_message(filters.private & filters.command("destination"))
async def cmd_destination(bot: Client, message: Message):
    dest, label = await db.get_destination(message.from_user.id)
    if dest:
        await message.reply(
            f"📤 **Current destination:** {label or dest}\n\n"
            f"Use /setchannel to change it, or /resetdest to send here.",
            reply_markup=kb_destination_menu(),
        )
    else:
        await message.reply(
            "📥 No destination set. Files are sent to **this chat**.\n\n"
            "Use /setchannel to send to a channel or group instead.",
            reply_markup=kb_destination_menu(),
        )


@Client.on_message(filters.private & filters.command("resetdest"))
async def cmd_resetdest(bot: Client, message: Message):
    await db.set_destination(message.from_user.id, None, None)
    await message.reply("✅ Destination reset. Files will be sent to **this chat**.")


@Client.on_message(filters.private & filters.command("setchannel"))
async def cmd_setchannel(bot: Client, message: Message):
    args = message.text.split(None, 1)
    if len(args) < 2:
        return await _ask_for_destination(bot, message)

    target = args[1].strip()
    await _try_set_destination(bot, message, target)


async def _ask_for_destination(bot: Client, message: Message):
    _WAITING_DEST_FORWARD.add(message.from_user.id)
    await message.reply(
        "📤 **Set destination chat**\n\n"
        "Pick one method:\n\n"
        "**Option 1:** Forward any message from the target channel/group to me\n\n"
        "**Option 2:** Send `@channelusername` for public channels\n\n"
        "**Option 3:** Send `-1001234567890` (chat ID) for private chats\n\n"
        "⚠️ The bot must be **added as admin** to the target channel/group with "
        "post permission.\n\n"
        "Send /cancel to abort."
    )


async def _try_set_destination(bot: Client, message: Message, target):
    """Resolve the target and verify bot can post there."""
    user_id = message.from_user.id

    # Try to convert to int (chat ID)
    if isinstance(target, str):
        target = target.strip()
        if target.lstrip("-").isdigit():
            try:
                target_id = int(target)
            except ValueError:
                target_id = target
        else:
            # username
            target_id = target if target.startswith("@") else "@" + target

    try:
        chat = await bot.get_chat(target_id)
    except (ChannelInvalid, PeerIdInvalid, BadRequest):
        return await message.reply(
            "❌ Could not access that chat.\n\n"
            "Make sure:\n"
            "• Bot is added to the chat\n"
            "• Bot is an admin with post permission\n"
            "• Username/ID is correct"
        )
    except Exception as e:
        return await message.reply(f"❌ Error: {e}")

    # Test we can post
    try:
        test = await bot.send_message(chat.id, "🔧 Destination test — you can delete this.")
        await test.delete()
    except Exception:
        return await message.reply(
            "❌ Bot can access the chat but can't post messages there.\n"
            "Please make the bot an **admin** with **post permission**."
        )

    label = f"@{chat.username}" if chat.username else chat.title or str(chat.id)
    await db.set_destination(user_id, chat.id, label)
    _WAITING_DEST_FORWARD.discard(user_id)

    await message.reply(
        f"✅ Destination set: **{label}**\n\n"
        f"All future downloads will be sent there.\n"
        f"Use /resetdest to switch back to this chat."
    )


# ── Catch forwarded message while in "waiting" state ──────────────────────────
@Client.on_message(filters.private & filters.forwarded)
async def handle_forward_for_dest(bot: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in _WAITING_DEST_FORWARD:
        return  # let other handlers process

    # Get the source chat from forwarded message
    if message.forward_from_chat:
        target_id = message.forward_from_chat.id
        await _try_set_destination(bot, message, target_id)
    else:
        await message.reply(
            "❌ Couldn't detect the source chat from that forward.\n"
            "Try sending the chat's @username or ID directly."
        )


# ── Callback buttons ──────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^dest_here$"))
async def cb_dest_here(bot: Client, cb: CallbackQuery):
    await db.set_destination(cb.from_user.id, None, None)
    await cb.message.edit("✅ Destination reset. Files will be sent to **this chat**.")


@Client.on_callback_query(filters.regex("^dest_set$"))
async def cb_dest_set(bot: Client, cb: CallbackQuery):
    await cb.message.delete()
    await _ask_for_destination(bot, cb.message)


@Client.on_callback_query(filters.regex("^dest_help$"))
async def cb_dest_help(bot: Client, cb: CallbackQuery):
    await cb.answer(
        "Add the bot to your channel/group as ADMIN with post permission, "
        "then forward any message from there to me.",
        show_alert=True,
    )
