"""
ClaudeSession — per-topic Claude Code bridge.

Architecture (revised):
  Claude CLI does NOT support a persistent stdin loop.
  Correct approach: each user message spawns one `claude -p --output-format
  stream-json -c` call in the topic's working directory.
  The `-c` flag continues the most recent session in that directory,
  preserving conversation history across calls.

Output pipeline:
  stdout → stream-json lines → parsed → text extracted → MessageBuffer → Telegram
  stderr → raw text → MessageBuffer (prefixed) → Telegram
"""
import asyncio
import base64
import json
import os
import signal
import time
from pathlib import Path
from typing import Callable, Awaitable, Optional

from bot.config import (
    CLAUDE_BIN,
    HTTP_PROXY, HTTPS_PROXY, ALL_PROXY,
    SEND_PROCESSING_HINT,
    PROCESSING_HINT_DELAY,
    LONG_WAIT_HINT_DELAY,
)
from bot.logger import get_logger
from bot.message_buffer import MessageBuffer

log = get_logger("claude_bridge.session")


def _build_env() -> dict:
    """Inherit environment, ensure claude binary dir is on PATH, inject proxy."""
    env = os.environ.copy()
    claude_dir = os.path.dirname(os.path.abspath(CLAUDE_BIN))
    if claude_dir and claude_dir not in env.get("PATH", ""):
        env["PATH"] = claude_dir + ":" + env.get("PATH", "")
    for key, val in [
        ("http_proxy", HTTP_PROXY),
        ("https_proxy", HTTPS_PROXY),
        ("all_proxy", ALL_PROXY),
    ]:
        if val:
            env[key] = val
            env[key.upper()] = val
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _extract_text_from_stream_json(line: str) -> str:
    """
    Parse one line of claude --output-format stream-json --verbose output.

    Event types we care about:
      assistant  — text reply + tool_use blocks
      tool_result — output from tool execution
      result     — final summary (success or error)
    """
    line = line.strip()
    if not line:
        return ""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line  # plain text fallback

    t = obj.get("type", "")

    if t == "assistant":
        parts = []
        for block in obj.get("message", {}).get("content", []):
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                # Show which tool is being called (brief)
                name = block.get("name", "tool")
                inp = block.get("input", {})
                # Show the most useful input field
                desc = (
                    inp.get("command")
                    or inp.get("query")
                    or inp.get("url")
                    or inp.get("prompt")
                    or inp.get("description")
                    or ""
                )
                if desc:
                    desc = str(desc)[:120]
                    parts.append(f"\n🔧 `{name}`: {desc}\n")
                else:
                    parts.append(f"\n🔧 `{name}`\n")
        return "".join(parts)

    if t == "tool_result":
        # Tool output — show a brief excerpt so user knows it ran
        content = obj.get("content", "")
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            content = "\n".join(texts)
        content = str(content).strip()
        if content:
            # Truncate very long tool output in Telegram (full output goes to log)
            preview = content[:300] + ("…" if len(content) > 300 else "")
            return f"```\n{preview}\n```\n"
        return ""

    if t == "result":
        if obj.get("is_error"):
            return f"\n❌ Claude 错误：{obj.get('result', '')}\n"
        # success result — already shown via assistant blocks, skip
        return ""

    return ""


class ClaudeSession:
    """
    Manages Claude interactions for one Telegram topic.

    Each call to `send()` launches a fresh `claude -p -c` subprocess,
    streams its output back to Telegram, then exits cleanly.
    Conversation history is maintained by Claude's own `-c` mechanism
    (stored in the working directory).
    """

    def __init__(
        self,
        key: str,
        working_dir: str,
        send_fn: Callable[[str], Awaitable[None]],
        on_exit: Callable[["ClaudeSession"], Awaitable[None]],
    ) -> None:
        self.key = key
        self.working_dir = working_dir
        self._send_fn = send_fn
        self._on_exit = on_exit

        self._current_proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()   # one message at a time per topic

        self.started_at: float = time.time()
        self.last_output_at: Optional[float] = None
        self._last_input_at: Optional[float] = None
        self._hint_task: Optional[asyncio.Task] = None
        self._active = True           # False after explicit stop()

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def pid(self) -> Optional[int]:
        p = self._current_proc
        return p.pid if p and p.returncode is None else None

    @property
    def is_running(self) -> bool:
        return self._active

    async def start(self) -> None:
        """No-op: session is ready as soon as it's constructed."""
        log.info("[%s] ClaudeSession ready (working_dir=%s)", self.key, self.working_dir)

    async def send(self, text: str) -> None:
        """Send one user message to Claude and stream the response back."""
        if not self._active:
            raise RuntimeError("Session has been stopped")
        async with self._lock:
            self._last_input_at = time.time()
            self.last_output_at = None
            if SEND_PROCESSING_HINT:
                self._schedule_hint()
            await self._run_claude(text)

    async def send_image(self, image_path: str, caption: str = "") -> None:
        """Send an image (with optional caption) to Claude via stream-json stdin."""
        if not self._active:
            raise RuntimeError("Session has been stopped")
        async with self._lock:
            self._last_input_at = time.time()
            self.last_output_at = None
            if SEND_PROCESSING_HINT:
                self._schedule_hint()
            await self._run_claude_with_image(image_path, caption)

    async def stop(self) -> None:
        """Cancel any in-flight subprocess."""
        self._active = False
        if self._hint_task and not self._hint_task.done():
            self._hint_task.cancel()
        proc = self._current_proc
        if proc and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        log.info("[%s] Session stopped", self.key)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_claude(self, prompt: str) -> None:
        """Spawn claude, stream output, send to Telegram."""
        env = _build_env()
        cmd = [
            CLAUDE_BIN,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--continue",
            "--permission-mode", "bypassPermissions",  # full tool access
            prompt,
        ]
        log.info("[%s] Running: %s", self.key, " ".join(cmd[:4]) + " ...")

        buf = MessageBuffer(self.key, self._send_fn)
        flush_stop = asyncio.Event()
        flush_task = asyncio.create_task(buf.flush_loop(flush_stop))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
                env=env,
                start_new_session=True,
                limit=10 * 1024 * 1024,  # 10MB — handles large image analysis JSON
            )
            self._current_proc = proc
            log.info("[%s] claude pid=%d", self.key, proc.pid)

            # Read stdout and stderr concurrently
            await asyncio.gather(
                self._read_stdout(proc.stdout, buf),
                self._read_stderr(proc.stderr, buf),
            )
            await proc.wait()
            log.info("[%s] claude exited code=%d", self.key, proc.returncode)

        except Exception as e:
            log.error("[%s] subprocess error: %s", self.key, e)
            buf.append(f"\n❌ 运行 Claude 出错：{e}")
        finally:
            flush_stop.set()
            await flush_task
            self._current_proc = None
            if self._hint_task and not self._hint_task.done():
                self._hint_task.cancel()

    async def _run_claude_with_image(self, image_path: str, caption: str) -> None:
        """Send image + caption to Claude via --input-format stream-json stdin."""
        env = _build_env()
        # Use stream-json input so we can pass image content blocks via stdin
        cmd = [
            CLAUDE_BIN,
            "--print",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--continue",
            "--permission-mode", "bypassPermissions",
        ]
        log.info("[%s] Running image cmd: %s", self.key, " ".join(cmd[:4]) + " ...")

        # Build the image content block
        try:
            img_bytes = Path(image_path).read_bytes()
            b64 = base64.standard_b64encode(img_bytes).decode()
            suffix = Path(image_path).suffix.lower()
            mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".png": "image/png", ".gif": "image/gif",
                    ".webp": "image/webp"}.get(suffix, "image/jpeg")
        except Exception as e:
            log.error("[%s] failed to read image %s: %s", self.key, image_path, e)
            return

        content = [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}]
        if caption:
            content.append({"type": "text", "text": caption})
        else:
            content.append({"type": "text", "text": "请分析这张图片。"})

        stdin_msg = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"

        buf = MessageBuffer(self.key, self._send_fn)
        flush_stop = asyncio.Event()
        flush_task = asyncio.create_task(buf.flush_loop(flush_stop))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
                env=env,
                start_new_session=True,
                limit=10 * 1024 * 1024,
            )
            self._current_proc = proc
            log.info("[%s] claude (image) pid=%d", self.key, proc.pid)

            # Write image message then close stdin
            proc.stdin.write(stdin_msg.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            await asyncio.gather(
                self._read_stdout(proc.stdout, buf),
                self._read_stderr(proc.stderr, buf),
            )
            await proc.wait()
            log.info("[%s] claude (image) exited code=%d", self.key, proc.returncode)

        except Exception as e:
            log.error("[%s] image subprocess error: %s", self.key, e)
            buf.append(f"\n❌ 图片分析出错：{e}")
        finally:
            flush_stop.set()
            await flush_task
            self._current_proc = None
            if self._hint_task and not self._hint_task.done():
                self._hint_task.cancel()

    async def _read_stdout(self, stream: asyncio.StreamReader, buf: MessageBuffer) -> None:
        """Read stream-json lines from stdout, extract text, feed buffer."""
        try:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace")
                text = _extract_text_from_stream_json(line)
                if text:
                    self.last_output_at = time.time()
                    log.debug("[%s] stdout text: %d chars", self.key, len(text))
                    buf.append(text)
                    await buf.flush()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("[%s] stdout reader error: %s", self.key, e)

    async def _read_stderr(self, stream: asyncio.StreamReader, buf: MessageBuffer) -> None:
        """Read stderr and forward as warning messages."""
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace").strip()
                if text:
                    log.warning("[%s] stderr: %s", self.key, text)
                    # Only forward real errors, not routine warnings
                    if any(kw in text.lower() for kw in ("error", "fatal", "exception", "traceback")):
                        buf.append(f"\n⚠️ [stderr] {text}\n")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("[%s] stderr reader error: %s", self.key, e)

    def _schedule_hint(self) -> None:
        if self._hint_task and not self._hint_task.done():
            self._hint_task.cancel()
        self._hint_task = asyncio.create_task(self._hint_timer())

    async def _hint_timer(self) -> None:
        try:
            await asyncio.sleep(PROCESSING_HINT_DELAY)
            if not self._has_output_since_last_input():
                await self._send_fn("⏳ Claude 仍在处理中，暂未产生可回传输出…")

            await asyncio.sleep(LONG_WAIT_HINT_DELAY - PROCESSING_HINT_DELAY)
            if not self._has_output_since_last_input():
                await self._send_fn(
                    "⚠️ Claude 超过 60 秒没有输出。\n"
                    "可能原因：任务复杂、进程卡住。\n"
                    "可发送 /restart 重启会话。"
                )
        except asyncio.CancelledError:
            pass

    def _has_output_since_last_input(self) -> bool:
        if self._last_input_at is None:
            return True
        if self.last_output_at is None:
            return False
        return self.last_output_at > self._last_input_at
