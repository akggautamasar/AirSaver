"""
/login  - OTP-based login, stores session in MongoDB
/logout - clears session
"""
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import (
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid,
    PhoneCodeExpired, SessionPasswordNeeded, PasswordHashInvalid,
)
from database.db import db
from helpers.keyboards import kb_login_help
from config import API_ID, API_HASH


@Client.on_message(filters.private & filters.command("logout"))
async def logout(client: Client, message: Message):
    if not await db.is_user_exist(message.from_user.id):
        return await message.reply("You are not logged in.")
    session = await db.get_session(message.from_user.id)
    if not session:
        return await message.reply("You are not logged in.")
    await db.set_session(message.from_user.id, None)
    await message.reply("✅ **Logged out successfully.**")


@Client.on_message(filters.private & filters.command("login"))
async def login(bot: Client, message: Message):
    user_id = message.from_user.id

    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    existing = await db.get_session(user_id)
    if existing:
        return await message.reply(
            "⚠️ You are already logged in.\n"
            "Use /logout first if you want to switch accounts."
        )

    await message.reply(
        "**Let's log you in to your Telegram account.**\n\n"
        "First, you'll need your API ID and API Hash from my.telegram.org.",
        reply_markup=kb_login_help(),
    )

    def _from_user(_, __, m):
        return m.from_user and m.from_user.id == user_id

    user_filter = filters.create(_from_user) & filters.private & filters.text

    async def ask(prompt: str, timeout: int = 300) -> Message | None:
        await bot.send_message(user_id, prompt)
        try:
            return await bot.listen(user_id, filters=user_filter, timeout=timeout)
        except asyncio.TimeoutError:
            await bot.send_message(user_id, "⏰ Timed out. Please start again with /login.")
            return None

    # ── API ID ─────────────────────────────────────────────────────────────────
    api_id_msg = await ask(
        "📌 Send your **API ID**.\n\n_(Send /skip to use the bot's default — higher ban risk)_"
    )
    if not api_id_msg:
        return

    if api_id_msg.text.strip() == "/skip":
        user_api_id = API_ID
        user_api_hash = API_HASH
    else:
        try:
            user_api_id = int(api_id_msg.text.strip())
        except ValueError:
            return await bot.send_message(user_id, "❌ API ID must be a number. Start again with /login.")

        api_hash_msg = await ask("📌 Now send your **API Hash**.")
        if not api_hash_msg:
            return
        user_api_hash = api_hash_msg.text.strip()

    # ── Phone number ───────────────────────────────────────────────────────────
    phone_msg = await ask(
        "📱 Send your **phone number** with country code.\n"
        "Example: `+911234567890`"
    )
    if not phone_msg:
        return
    phone = phone_msg.text.strip()

    acc = Client(":memory:", user_api_id, user_api_hash)
    try:
        await acc.connect()
    except ApiIdInvalid:
        return await bot.send_message(user_id, "❌ Invalid API ID / Hash. Start again with /login.")

    try:
        sent_code = await acc.send_code(phone)
    except PhoneNumberInvalid:
        await acc.disconnect()
        return await bot.send_message(user_id, "❌ Invalid phone number. Start again with /login.")

    # ── OTP ────────────────────────────────────────────────────────────────────
    otp_msg = await ask(
        "🔑 An OTP was sent to your Telegram account.\n\n"
        "Send it **with spaces** between digits.\n"
        "Example: if OTP is `12345` → send `1 2 3 4 5`\n\n"
        "_(Send /cancel to abort)_",
        timeout=600,
    )
    if not otp_msg:
        await acc.disconnect()
        return
    if otp_msg.text.strip() == "/cancel":
        await acc.disconnect()
        return await bot.send_message(user_id, "❌ Login cancelled.")

    otp = otp_msg.text.strip().replace(" ", "")

    try:
        await acc.sign_in(phone, sent_code.phone_code_hash, otp)
    except PhoneCodeInvalid:
        await acc.disconnect()
        return await bot.send_message(user_id, "❌ Wrong OTP. Start again with /login.")
    except PhoneCodeExpired:
        await acc.disconnect()
        return await bot.send_message(user_id, "❌ OTP expired. Start again with /login.")
    except SessionPasswordNeeded:
        # ── 2FA ───────────────────────────────────────────────────────────────
        tfa_msg = await ask(
            "🔒 Your account has **2-step verification** enabled.\n"
            "Send your password:\n_(Send /cancel to abort)_",
            timeout=300,
        )
        if not tfa_msg:
            await acc.disconnect()
            return
        if tfa_msg.text.strip() == "/cancel":
            await acc.disconnect()
            return await bot.send_message(user_id, "❌ Login cancelled.")
        try:
            await acc.check_password(tfa_msg.text.strip())
        except PasswordHashInvalid:
            await acc.disconnect()
            return await bot.send_message(user_id, "❌ Wrong password. Start again with /login.")

    session_string = await acc.export_session_string()
    await acc.disconnect()

    await db.set_session(user_id, session_string)
    await db.set_api_id(user_id, user_api_id)
    await db.set_api_hash(user_id, user_api_hash)

    await bot.send_message(
        user_id,
        "✅ **Logged in successfully!**\n\n"
        "Now send any Telegram post link to get started.\n"
        "Use /help to see all options."
    )
