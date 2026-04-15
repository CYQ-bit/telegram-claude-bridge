"""
Logging setup — three separate log files plus console output.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from bot.config import LOGS_DIR, LOG_LEVEL

FMT = "%(asctime)s [%(levelname)s] %(name)s | %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _make_handler(filename: str) -> RotatingFileHandler:
    h = RotatingFileHandler(
        LOGS_DIR / filename,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter(FMT, DATE_FMT))
    return h


def _console_handler() -> logging.StreamHandler:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(FMT, DATE_FMT))
    return h


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)

    # Root logger — app.log + console
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(_make_handler("app.log"))
    root.addHandler(_console_handler())

    # Telegram-specific logger
    tg_logger = logging.getLogger("telegram_bridge")
    tg_logger.addHandler(_make_handler("telegram.log"))

    # Claude-specific logger
    cl_logger = logging.getLogger("claude_bridge")
    cl_logger.addHandler(_make_handler("claude.log"))

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
