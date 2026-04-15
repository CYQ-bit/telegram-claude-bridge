"""
Utility helpers — ANSI stripping, text chunking, path validation.
"""
import re
from pathlib import Path

# Matches ANSI escape sequences (colours, cursor movement, etc.)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHFABCDJsu]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")
# Matches other non-printable control chars except newline/tab
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and stray control characters."""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text


def chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks that fit within Telegram's message limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split on a newline boundary
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def validate_directory(path_str: str) -> tuple[bool, str]:
    """
    Returns (ok, resolved_path_or_error_message).
    Expands ~ and resolves the path.
    """
    try:
        p = Path(path_str.strip()).expanduser().resolve()
        if not p.exists():
            return False, f"路径不存在: {p}"
        if not p.is_dir():
            return False, f"不是目录: {p}"
        return True, str(p)
    except Exception as e:
        return False, f"路径无效: {e}"


def topic_key(chat_id: int, thread_id: int) -> str:
    return f"{chat_id}:{thread_id}"
