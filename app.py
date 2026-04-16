"""
app.py — entry point.

Starts the Telegram bot with:
  - Proxy injected into the HTTP client (fixes ConnectTimeout)
  - Auto-reconnect on network errors
  - Graceful shutdown on SIGINT/SIGTERM
  - Retry loop: waits for proxy/network before giving up
"""
import asyncio
import signal
import sys
from typing import Optional
from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut

from bot.config import HTTPS_PROXY, HTTP_PROXY
from bot.logger import setup_logging, get_logger
from bot.telegram_bot import TelegramBot

log = get_logger("app")

_PROXY = HTTPS_PROXY or HTTP_PROXY
_stop_event: Optional[asyncio.Event] = None


def _handle_signal():
    log.info("Shutdown signal received")
    if _stop_event:
        _stop_event.set()


async def main() -> None:
    global _stop_event
    setup_logging()
    log.info("Starting Telegram-Claude Bridge…")
    log.info("Proxy: %s", _PROXY or "none")

    loop = asyncio.get_running_loop()
    _stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    retry_delay = 15  # seconds between retries when network is unavailable

    while not _stop_event.is_set():
        bridge = TelegramBot()
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
                read_timeout=60.0,
                write_timeout=30.0,
                pool_timeout=30.0,
            )
            builder = builder.request(request).get_updates_request(get_updates_request)
        else:
            builder = builder.connection_pool_size(16).connect_timeout(30).read_timeout(30)

        app = builder.build()
        bridge.set_app(app)
        bridge.register_handlers()

        try:
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
                await _stop_event.wait()
                log.info("Shutting down…")
                await app.updater.stop()
                await app.stop()
                await bridge.shutdown()
            break  # clean shutdown, exit loop

        except (NetworkError, TimedOut, OSError) as e:
            if _stop_event.is_set():
                break
            log.warning("Network error: %s — retrying in %ds…", e, retry_delay)
            await asyncio.sleep(retry_delay)

        except Exception as e:
            if _stop_event.is_set():
                break
            log.error("Unexpected error: %s — retrying in %ds…", e, retry_delay)
            await asyncio.sleep(retry_delay)

    log.info("Bye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
