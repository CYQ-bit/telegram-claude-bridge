"""
app.py — entry point.

Starts the Telegram bot with:
  - Proxy injected into the HTTP client (fixes ConnectTimeout)
  - Auto-reconnect on network errors
  - Graceful shutdown on SIGINT/SIGTERM
"""
import asyncio
import signal
import sys
from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest

from bot.config import HTTPS_PROXY, HTTP_PROXY
from bot.logger import setup_logging, get_logger
from bot.telegram_bot import TelegramBot

log = get_logger("app")

# Use HTTPS_PROXY first, fall back to HTTP_PROXY
_PROXY = HTTPS_PROXY or HTTP_PROXY


async def main() -> None:
    setup_logging()
    log.info("Starting Telegram-Claude Bridge…")
    log.info("Proxy: %s", _PROXY or "none")

    bridge = TelegramBot()

    # Build Application with proxy-aware HTTP client and generous timeouts
    builder = ApplicationBuilder().token(bridge._get_token())

    if _PROXY:
        request = HTTPXRequest(
            proxy=_PROXY,
            connection_pool_size=16,
            connect_timeout=30.0,
            read_timeout=30.0,
            write_timeout=30.0,
            pool_timeout=30.0,
        )
        get_updates_request = HTTPXRequest(
            proxy=_PROXY,
            connection_pool_size=4,
            connect_timeout=30.0,
            read_timeout=60.0,   # long-poll needs longer read timeout
            write_timeout=30.0,
            pool_timeout=30.0,
        )
        builder = builder.request(request).get_updates_request(get_updates_request)
    else:
        builder = builder.connection_pool_size(16).connect_timeout(30).read_timeout(30)

    app = builder.build()
    bridge.set_app(app)
    bridge.register_handlers()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    async with app:
        await app.initialize()
        await bridge.set_commands()
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        log.info("Bot is running.")
        bridge._scheduler.start()
        await stop_event.wait()
        log.info("Shutting down…")
        await app.updater.stop()
        await app.stop()
        await bridge.shutdown()

    log.info("Bye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
