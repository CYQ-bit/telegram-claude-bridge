"""
MessageBuffer — aggregates Claude output and flushes to Telegram in batches.

Design goals:
  - Never block the reader coroutine
  - Flush every OUTPUT_FLUSH_INTERVAL seconds (or when buffer is large)
  - Split oversized messages automatically
  - Retry on Telegram errors
  - Log every step so "no output" bugs are diagnosable
"""
import asyncio
import time
from typing import Callable, Awaitable

from bot.config import OUTPUT_FLUSH_INTERVAL, MAX_TELEGRAM_MSG_LEN
from bot.logger import get_logger
from bot.utils import chunk_text, strip_ansi

log = get_logger("telegram_bridge.buffer")

# Flush immediately if buffer exceeds this size (avoid very long messages)
EAGER_FLUSH_SIZE = MAX_TELEGRAM_MSG_LEN * 2


class MessageBuffer:
    """
    Per-topic output buffer.

    Usage:
        buf = MessageBuffer(key, send_fn)
        buf.append("some text")
        # background flush_loop() drains it periodically
    """

    def __init__(
        self,
        key: str,
        send_fn: Callable[[str], Awaitable[None]],
    ) -> None:
        self.key = key
        self._send = send_fn
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()
        self._total_received = 0
        self._total_flushed = 0

    def append(self, text: str) -> None:
        """Add raw text (will be cleaned before sending)."""
        cleaned = strip_ansi(text)
        if not cleaned:
            return
        self._buf.append(cleaned)
        self._total_received += len(cleaned)
        log.debug("[%s] buffer +%d chars (total buffered=%d)", self.key, len(cleaned), self._buffered_len())

    def _buffered_len(self) -> int:
        return sum(len(s) for s in self._buf)

    async def flush(self, force: bool = False) -> None:
        """Send buffered content to Telegram if there is anything."""
        async with self._lock:
            if not self._buf:
                return
            now = time.monotonic()
            elapsed = now - self._last_flush
            if not force and elapsed < OUTPUT_FLUSH_INTERVAL and self._buffered_len() < EAGER_FLUSH_SIZE:
                return

            combined = "".join(self._buf).strip()
            self._buf.clear()
            self._last_flush = now

            if not combined:
                return

            self._total_flushed += len(combined)
            log.info("[%s] flushing %d chars to Telegram (total_flushed=%d)", self.key, len(combined), self._total_flushed)

            for chunk in chunk_text(combined, MAX_TELEGRAM_MSG_LEN):
                await self._send_with_retry(chunk)

    async def _send_with_retry(self, text: str, retries: int = 3) -> None:
        for attempt in range(1, retries + 1):
            try:
                await self._send(text)
                log.debug("[%s] Telegram send OK (attempt %d)", self.key, attempt)
                return
            except Exception as e:
                log.warning("[%s] Telegram send failed attempt %d: %s", self.key, attempt, e)
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)
        log.error("[%s] Telegram send FAILED after %d attempts", self.key, retries)

    async def flush_loop(self, stop_event: asyncio.Event) -> None:
        """Background coroutine — flush periodically until stop_event is set."""
        log.debug("[%s] flush_loop started", self.key)
        while not stop_event.is_set():
            await asyncio.sleep(OUTPUT_FLUSH_INTERVAL)
            await self.flush()
        # Final drain
        await self.flush(force=True)
        log.debug("[%s] flush_loop stopped (total_received=%d total_flushed=%d)",
                  self.key, self._total_received, self._total_flushed)
