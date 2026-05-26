import os
import asyncio
import shutil
from aiohttp import web

# ── uvloop: faster asyncio event loop (~10-15% speedup) ────────────────────────
try:
    import uvloop
    uvloop.install()
    _UVLOOP_OK = True
except ImportError:
    _UVLOOP_OK = False

from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN, STRING_SESSION, LOGIN_SYSTEM
from logger import LOGGER


# ── Shared user client (only when LOGIN_SYSTEM=False) ─────────────────────────
TechVJUser = None
if not LOGIN_SYSTEM and STRING_SESSION:
    TechVJUser = Client(
        "shared_user",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=STRING_SESSION,
        in_memory=True,
    )


# ── Health check server ───────────────────────────────────────────────────────
async def health(request):
    return web.Response(text="OK")


async def run_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    LOGGER(__name__).info(f"Health check server on port {port}")


# ── Bot class ─────────────────────────────────────────────────────────────────
class AirSaverBot(Client):
    def __init__(self):
        from config import MAX_TRANSMISSIONS
        super().__init__(
            "airsaver_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=dict(root="plugins"),
            workers=200,
            sleep_threshold=10,
            in_memory=True,
            max_concurrent_transmissions=MAX_TRANSMISSIONS,
        )

    async def start(self):
        await super().start()
        me = await self.get_me()
        LOGGER(__name__).info(f"Bot started: @{me.username}")

    async def stop(self, *args):
        await super().stop()
        LOGGER(__name__).info("Bot stopped.")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Clean download dirs on startup
    for d in ["downloads", "thumbs"]:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    LOGGER(__name__).info(f"uvloop: {'enabled' if _UVLOOP_OK else 'NOT installed (fallback to asyncio)'}")

    async def main():
        await run_health_server()

        if TechVJUser is not None:
            await TechVJUser.start()
            LOGGER(__name__).info("Shared user client started.")

        bot = AirSaverBot()
        await bot.start()

        LOGGER(__name__).info("All systems running. Press Ctrl+C to stop.")
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER(__name__).info("Shutting down...")
