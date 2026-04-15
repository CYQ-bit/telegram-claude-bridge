"""
Configuration loader — reads from .env and validates required fields.
"""
import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _parse_id_list(raw: str) -> set[int]:
    """Parse comma-separated integer IDs."""
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                pass
    return result


def _detect_claude_bin() -> str:
    """Auto-detect claude binary path."""
    # 1. Explicit env override
    env_bin = os.getenv("CLAUDE_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin

    # 2. which claude
    found = shutil.which("claude")
    if found:
        return found

    # 3. Common macOS install locations
    candidates = [
        Path.home() / ".local/bin/claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    raise RuntimeError(
        "Cannot find 'claude' binary. Set CLAUDE_BIN in .env or ensure claude is on PATH."
    )


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

ALLOWED_CHAT_IDS: set[int] = _parse_id_list(os.getenv("ALLOWED_CHAT_IDS", ""))
ALLOWED_USER_IDS: set[int] = _parse_id_list(os.getenv("ALLOWED_USER_IDS", ""))

# ── Claude ────────────────────────────────────────────────────────────────────
CLAUDE_BIN: str = _detect_claude_bin()
DEFAULT_BASE_DIR: str = os.getenv("DEFAULT_BASE_DIR", str(Path.home()))

# ── Proxy (inherited by subprocesses) ────────────────────────────────────────
HTTP_PROXY: str = os.getenv("HTTP_PROXY", os.getenv("http_proxy", ""))
HTTPS_PROXY: str = os.getenv("HTTPS_PROXY", os.getenv("https_proxy", ""))
ALL_PROXY: str = os.getenv("ALL_PROXY", os.getenv("all_proxy", ""))

# ── Behaviour tuning ──────────────────────────────────────────────────────────
SEND_PROCESSING_HINT: bool = os.getenv("SEND_PROCESSING_HINT", "true").lower() == "true"
PROCESSING_HINT_DELAY: float = float(os.getenv("PROCESSING_HINT_DELAY", "10"))
LONG_WAIT_HINT_DELAY: float = float(os.getenv("LONG_WAIT_HINT_DELAY", "60"))
OUTPUT_FLUSH_INTERVAL: float = float(os.getenv("OUTPUT_FLUSH_INTERVAL", "1.5"))
MAX_TELEGRAM_MSG_LEN: int = int(os.getenv("MAX_TELEGRAM_MSG_LEN", "3500"))

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
STATE_FILE = DATA_DIR / "topic_state.json"

DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
