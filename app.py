"""
app.py — entry point.

Starts the Telegram bot with:
  - Proxy injected into the HTTP client (fixes ConnectTimeout)
  - Auto-reconnect on network errors
  - Graceful shutdown on SIGINT/SIGTERM
  - Health-check watchdog: detects dead connections and forces restart
"""
import asyncio
import signal
import sys
from typing import Optional
from telegram.ext import ApplicationBuilder, Application
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TimedOut

from bot.config import HTTPS_PROXY, HTTP_PROXY
from bot.logger import setup_logging, get_logger
from bot.telegram_bot import TelegramBot

log = get_logger("app")

_PROXY = HTTPS_PROXY or HTTP_PROXY
_stop_event: Optional[asyncio.Event] = None

HEALTH_CHECK_INTERVAL = 90
MAX_CONSECUTIVE_FAILURES = 3


def _handle_signal():
    log.info("Shutdown signal received")
    if _stop_event:
        _stop_event.set()


async def _health_check(app: Application, restart_event: asyncio.Event) -> None:
    """Periodically call getMe to verify the connection is alive.
    Sets restart_event after MAX_CONSECUTIVE_FAILURES consecutive failures."""
    failures = 0
    while not _stop_event.is_set() and not restart_event.is_set():
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        if _stop_event.is_set():
            return
        try:
            await app.bot.get_me()
            if failures > 0:
                log.info("Health check recovered after %d failure(s)", failures)
            failures = 0
        except Exception as e:
            failures += 1
            log.warning("Health check failed (%d/%d): %s", failures, MAX_CONSECUTIVE_FAILURES, e)
            if failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("Health check failed %d times — forcing restart", failures)
                restart_event.set()
                return


async def main() -> None:
    global _stop_event
    setup_logging()
    log.info("Starting Telegram-Claude Bridge…")
    log.info("Proxy: %s", _PROXY or "none")

    loop = asyncio.get_running_loop()
    _stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    retry_delay = 15

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

        restart_event = asyncio.Event()
        watchdog_task: Optional[asyncio.Task] = None

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

                watchdog_task = asyncio.create_task(_health_check(app, restart_event))

                stop_task = asyncio.create_task(_stop_event.wait())
                restart_task = asyncio.create_task(restart_event.wait())
                done, _ = await asyncio.wait(
                    [stop_task, restart_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stop_task.cancel()
                restart_task.cancel()

                if _stop_event.is_set():
                    log.info("Shutting down…")
                else:
                    log.warning("Watchdog triggered restart — tearing down…")

                watchdog_task.cancel()
                await app.updater.stop()
                await app.stop()
                await bridge.shutdown()

            if _stop_event.is_set():
                break
            log.info("Restarting in %ds…", retry_delay)
            await asyncio.sleep(retry_delay)

        except (NetworkError, TimedOut, OSError) as e:
            if watchdog_task:
                watchdog_task.cancel()
            if _stop_event.is_set():
                break
            log.warning("Network error: %s — retrying in %ds…", e, retry_delay)
            await asyncio.sleep(retry_delay)

        except Exception as e:
            if watchdog_task:
                watchdog_task.cancel()
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
