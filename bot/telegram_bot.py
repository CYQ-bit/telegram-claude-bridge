"""
TelegramBot — main handler wiring Telegram updates to sessions.

Handles:
  - Security checks (chat_id / user_id whitelist)
  - Topic-only enforcement
  - Directory selection flow (InlineKeyboard + manual input)
  - All slash commands
  - Plain text → Claude stdin
  - Image/photo → Claude with temp file path
  - Auto file detection and sending after Claude finishes
"""
import asyncio
import os
import time
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram import Document, PhotoSize
from telegram.error import TelegramError, RetryAfter

from bot.config import (
    BOT_TOKEN,
    ALLOWED_CHAT_IDS,
    ALLOWED_USER_IDS,
    DEFAULT_BASE_DIR,
    MAX_TELEGRAM_MSG_LEN,
)
from bot.logger import get_logger
from bot.topic_state import TopicStateStore
from bot.session_manager import SessionManager
from bot.scheduler import ScheduleStore, Scheduler
from bot.utils import validate_directory, topic_key, chunk_text

log = get_logger("telegram_bridge.bot")

_QUICK_DIRS = [
    ("~", "~"),
    ("Desktop", "~/Desktop"),
    ("Desktop/claude", "~/Desktop/claude"),
    ("Documents", "~/Documents"),
    ("Downloads", "~/Downloads"),
    ("claude_work", "~/claude_work"),
    ("Projects", "~/Projects"),
]

# File extensions we'll auto-send after Claude finishes a task
_SENDABLE_EXTENSIONS = {
    ".xlsx", ".xls", ".csv",
    ".pdf", ".docx", ".doc",
    ".png", ".jpg", ".jpeg", ".gif",
    ".zip", ".tar", ".gz",
    ".py", ".js", ".ts", ".json", ".txt", ".md",
    ".mp4", ".mov",
}

# Max file size to auto-send (50 MB)
_MAX_FILE_SIZE = 50 * 1024 * 1024


def _is_allowed(update: Update) -> bool:
    """Return True if the update comes from an allowed chat and user."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    user_id = update.effective_user.id if update.effective_user else None

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        log.warning("Rejected chat_id=%s", chat_id)
        return False
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        log.warning("Rejected user_id=%s", user_id)
        return False
    return True


def _get_thread_id(update: Update) -> Optional[int]:
    """Return message_thread_id or None for non-topic messages."""
    msg = update.effective_message
    if msg is None:
        return None
    return msg.message_thread_id


async def _safe_reply(update: Update, text: str, **kwargs) -> None:
    """Reply with automatic chunking and flood-wait handling."""
    for chunk in chunk_text(text, MAX_TELEGRAM_MSG_LEN):
        for attempt in range(3):
            try:
                await update.effective_message.reply_text(chunk, **kwargs)
                break
            except RetryAfter as e:
                log.warning("FloodWait %ds", e.retry_after)
                await asyncio.sleep(e.retry_after + 1)
            except TelegramError as e:
                log.error("Reply failed attempt %d: %s", attempt + 1, e)
                await asyncio.sleep(2)


class TelegramBot:
    def __init__(self) -> None:
        self._state_store = TopicStateStore()
        self._session_mgr = SessionManager(self._state_store)
        self._schedule_store = ScheduleStore()
        self._scheduler = Scheduler(self._schedule_store)
        self._app: Optional[Application] = None

    def _get_token(self) -> str:
        return BOT_TOKEN

    def set_app(self, app: Application) -> None:
        self._app = app
        # Wire scheduler's run function
        self._scheduler.set_run_fn(self._run_scheduled_prompt)

    # ── Build & run ───────────────────────────────────────────────────────────

    def build(self) -> Application:
        """Legacy single-call build (used when no proxy needed)."""
        from telegram.ext import ApplicationBuilder
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        self.set_app(app)
        self.register_handlers()
        return app

    def register_handlers(self) -> None:
        app = self._app
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("help", self._cmd_help))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("cwd", self._cmd_cwd))
        app.add_handler(CommandHandler("resetdir", self._cmd_resetdir))
        app.add_handler(CommandHandler("setdir", self._cmd_setdir))
        app.add_handler(CommandHandler("restart", self._cmd_restart))
        app.add_handler(CommandHandler("topics", self._cmd_topics))
        app.add_handler(CommandHandler("ping", self._cmd_ping))
        app.add_handler(CommandHandler("schedule", self._cmd_schedule))
        app.add_handler(CallbackQueryHandler(self._cb_dir_select))
        app.add_handler(MessageHandler(filters.PHOTO, self._on_image))
        app.add_handler(MessageHandler(filters.Document.IMAGE, self._on_image))
        app.add_handler(MessageHandler(filters.Document.ALL, self._on_image))  # catch all docs
        app.add_handler(MessageHandler(filters.Sticker.ALL, self._on_image))  # webp stickers
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

    async def set_commands(self) -> None:
        cmds = [
            BotCommand("start", "欢迎信息"),
            BotCommand("help", "命令列表"),
            BotCommand("status", "当前 Topic 状态"),
            BotCommand("cwd", "当前工作目录"),
            BotCommand("resetdir", "重置工作目录"),
            BotCommand("setdir", "直接设置工作目录 /setdir <路径>"),
            BotCommand("restart", "重启 Claude 会话"),
            BotCommand("topics", "列出所有 Topic（管理员）"),
            BotCommand("ping", "Bot 在线检测"),
            BotCommand("schedule", "定时任务管理 /schedule add/list/del/on/off"),
        ]
        await self._app.bot.set_my_commands(cmds)

    async def shutdown(self) -> None:
        await self._scheduler.stop()
        await self._session_mgr.stop_all()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_send_fn(self, chat_id: int, thread_id: int):
        """Return an async callable that sends text to the correct topic."""
        async def send(text: str) -> None:
            for chunk in chunk_text(text, MAX_TELEGRAM_MSG_LEN):
                for attempt in range(3):
                    try:
                        await self._app.bot.send_message(
                            chat_id=chat_id,
                            text=chunk,
                            message_thread_id=thread_id,
                        )
                        log.debug("[%s:%s] sent %d chars", chat_id, thread_id, len(chunk))
                        return
                    except RetryAfter as e:
                        log.warning("FloodWait %ds", e.retry_after)
                        await asyncio.sleep(e.retry_after + 1)
                    except TelegramError as e:
                        log.error("send_message failed attempt %d: %s", attempt + 1, e)
                        await asyncio.sleep(2 ** attempt)
        return send

    async def _send_new_files(
        self,
        chat_id: int,
        thread_id: int,
        working_dir: str,
        since_ts: float,
    ) -> None:
        """
        Scan working_dir for files created/modified after since_ts and send them.
        Silently skips on PermissionError (macOS sandbox restrictions).
        """
        try:
            wd = Path(working_dir)
            new_files = []
            try:
                entries = list(wd.iterdir())
            except PermissionError:
                log.warning("[%s:%s] No permission to scan %s (macOS sandbox)", chat_id, thread_id, working_dir)
                return
            for f in entries:
                try:
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in _SENDABLE_EXTENSIONS:
                        continue
                    if f.stat().st_mtime >= since_ts:
                        size = f.stat().st_size
                        if size <= _MAX_FILE_SIZE:
                            new_files.append(f)
                except PermissionError:
                    continue

            for f in sorted(new_files, key=lambda x: x.stat().st_mtime):
                log.info("[%s:%s] auto-sending file: %s", chat_id, thread_id, f.name)
                for attempt in range(3):
                    try:
                        with open(f, "rb") as fh:
                            await self._app.bot.send_document(
                                chat_id=chat_id,
                                document=fh,
                                filename=f.name,
                                message_thread_id=thread_id,
                                caption=f"📎 {f.name}",
                            )
                        break
                    except RetryAfter as e:
                        await asyncio.sleep(e.retry_after + 1)
                    except TelegramError as e:
                        log.error("send_document failed attempt %d: %s", attempt + 1, e)
                        await asyncio.sleep(2 ** attempt)
        except Exception as e:
            log.error("[%s:%s] _send_new_files error: %s", chat_id, thread_id, e)

    def _dir_keyboard(self) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton(label, callback_data=f"dir:{path}")]
            for label, path in _QUICK_DIRS
        ]
        buttons.append([InlineKeyboardButton("✏️ 手动输入路径", callback_data="dir:__manual__")])
        return InlineKeyboardMarkup(buttons)

    async def _prompt_dir(self, update: Update, state) -> None:
        state.status = "waiting_dir"
        state.pending_dir_input = False
        self._state_store.update(state)
        await _safe_reply(
            update,
            "📁 请选择工作目录，或点击「手动输入路径」：",
            reply_markup=self._dir_keyboard(),
        )

    async def _start_claude_for_state(self, update: Update, state) -> None:
        send_fn = self._make_send_fn(state.chat_id, state.thread_id)
        try:
            await self._session_mgr.get_or_start(state, send_fn)
            await _safe_reply(
                update,
                f"✅ Claude 会话已启动\n📂 工作目录：`{state.working_directory}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("[%s] start failed: %s", state.key(), e)

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        await _safe_reply(
            update,
            "👋 欢迎使用 Claude Code 远程桥接 Bot！\n\n"
            "请在群组的某个 **Topic（话题）** 内发消息。\n"
            "每个 Topic 对应一个独立的 Claude Code 会话。\n\n"
            "发送 /help 查看命令列表。",
            parse_mode="Markdown",
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        await _safe_reply(
            update,
            "📖 *命令列表*\n\n"
            "/status — 当前 Topic 状态\n"
            "/cwd — 当前工作目录\n"
            "/resetdir — 重置工作目录（弹出选择菜单）\n"
            "/setdir <路径> — 直接设置工作目录\n"
            "/restart — 重启 Claude 会话\n"
            "/topics — 列出所有 Topic（管理员）\n"
            "/ping — Bot 在线检测\n\n"
            "直接发文字消息即可与 Claude 对话。\n"
            "发送图片/截图，Claude 会自动分析。\n\n"
            "定时任务：\n"
            "`/schedule add <cron> <提示词>` — 添加定时任务\n"
            "`/schedule list` — 查看本 Topic 的定时任务\n"
            "`/schedule del <id>` — 删除定时任务\n"
            "`/schedule on/off <id>` — 启用/禁用\n"
            "cron 格式：分 时 日 月 周（如 `0 9 * * *` = 每天9点）",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(update, "⚠️ 请在 Topic 内使用此命令。")
            return
        chat_id = update.effective_chat.id
        state = self._state_store.get(chat_id, thread_id)
        if not state:
            await _safe_reply(update, "ℹ️ 此 Topic 尚未初始化。发送任意消息开始。")
            return
        session = self._session_mgr.get_session(state.key())
        running = session.is_running if session else False
        last = time.strftime("%H:%M:%S", time.localtime(state.last_activity))
        await _safe_reply(
            update,
            f"📊 *Topic 状态*\n\n"
            f"目录：`{state.working_directory or '未设置'}`\n"
            f"状态：{'🟢 运行中' if running else '🔴 未运行'}\n"
            f"PID：{state.pid or 'N/A'}\n"
            f"最近活动：{last}",
            parse_mode="Markdown",
        )

    async def _cmd_cwd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(update, "⚠️ 请在 Topic 内使用此命令。")
            return
        state = self._state_store.get(update.effective_chat.id, thread_id)
        cwd = state.working_directory if state else None
        await _safe_reply(update, f"📂 当前目录：`{cwd or '未设置'}`", parse_mode="Markdown")

    async def _cmd_resetdir(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(update, "⚠️ 请在 Topic 内使用此命令。")
            return
        chat_id = update.effective_chat.id
        state = self._state_store.get_or_create(chat_id, thread_id)
        # Stop existing session
        session = self._session_mgr.get_session(state.key())
        if session:
            await session.stop()
        state.working_directory = None
        state.status = "idle"
        state.pid = None
        self._state_store.update(state)
        await _safe_reply(update, "🔄 工作目录已重置。发送任意消息重新选择目录。")

    async def _cmd_restart(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(update, "⚠️ 请在 Topic 内使用此命令。")
            return
        chat_id = update.effective_chat.id
        state = self._state_store.get(chat_id, thread_id)
        if not state or not state.working_directory:
            await _safe_reply(update, "⚠️ 此 Topic 尚未设置工作目录，无法重启。")
            return
        await _safe_reply(update, "🔄 正在重启 Claude 会话…")
        send_fn = self._make_send_fn(chat_id, thread_id)
        try:
            await self._session_mgr.restart(state, send_fn)
            await _safe_reply(update, f"✅ Claude 会话已重启\n📂 目录：`{state.working_directory}`", parse_mode="Markdown")
        except Exception as e:
            await _safe_reply(update, f"❌ 重启失败：{e}")

    async def _cmd_topics(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        # Only allow if user is in ALLOWED_USER_IDS (treat as admin)
        states = self._state_store.all_states()
        if not states:
            await _safe_reply(update, "ℹ️ 暂无活跃 Topic。")
            return
        lines = ["📋 *所有 Topic 会话*\n"]
        for s in states:
            sess = self._session_mgr.get_session(s.key())
            running = sess.is_running if sess else False
            lines.append(
                f"• `{s.key()}` — {'🟢' if running else '🔴'} {s.working_directory or '未设置目录'}"
            )
        await _safe_reply(update, "\n".join(lines), parse_mode="Markdown")

    async def _cmd_ping(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return
        await _safe_reply(update, "🏓 Pong! Bot 在线。")

    async def _cmd_schedule(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """管理定时任务。用法：/schedule add <cron> <prompt> | list | del <id> | on <id> | off <id>"""
        if not _is_allowed(update):
            return
        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(update, "⚠️ 请在 Topic 内使用此命令。")
            return
        chat_id = update.effective_chat.id
        args = ctx.args or []

        if not args:
            await _safe_reply(update, "用法：`/schedule add <cron> <提示词>` | `list` | `del <id>` | `on/off <id>`", parse_mode="Markdown")
            return

        sub = args[0].lower()

        if sub == "list":
            schedules = self._schedule_store.list_for_topic(chat_id, thread_id)
            if not schedules:
                await _safe_reply(update, "ℹ️ 此 Topic 暂无定时任务。")
                return
            lines = ["📅 *定时任务列表*\n"]
            for s in schedules:
                status = "🟢" if s.enabled else "🔴"
                lines.append(f"{status} `{s.id}` — `{s.cron}`\n   {s.prompt[:60]}")
            await _safe_reply(update, "\n".join(lines), parse_mode="Markdown")

        elif sub == "add":
            # /schedule add M H DOM MON DOW prompt text...
            if len(args) < 7:
                await _safe_reply(update, "用法：`/schedule add <分> <时> <日> <月> <周> <提示词>`\n例：`/schedule add 0 9 * * * 早报：今日天气`", parse_mode="Markdown")
                return
            cron = " ".join(args[1:6])
            prompt = " ".join(args[6:])
            s = self._schedule_store.add(chat_id, thread_id, cron, prompt)
            await _safe_reply(update, f"✅ 定时任务已添加\nID：`{s.id}`\nCron：`{s.cron}`\n提示词：{s.prompt}", parse_mode="Markdown")

        elif sub == "del":
            if len(args) < 2:
                await _safe_reply(update, "用法：`/schedule del <id>`", parse_mode="Markdown")
                return
            ok = self._schedule_store.delete(args[1])
            await _safe_reply(update, "✅ 已删除。" if ok else "❌ 未找到该 ID。")

        elif sub in ("on", "off"):
            if len(args) < 2:
                await _safe_reply(update, f"用法：`/schedule {sub} <id>`", parse_mode="Markdown")
                return
            enabled = sub == "on"
            ok = self._schedule_store.set_enabled(args[1], enabled)
            label = "启用" if enabled else "禁用"
            await _safe_reply(update, f"✅ 已{label}。" if ok else "❌ 未找到该 ID。")

        else:
            await _safe_reply(update, "未知子命令。用法：`add` | `list` | `del` | `on` | `off`", parse_mode="Markdown")

    async def _run_scheduled_prompt(self, chat_id: int, thread_id: int, prompt: str) -> None:
        """Called by Scheduler to fire a prompt for a topic."""
        state = self._state_store.get(chat_id, thread_id)
        if not state or not state.working_directory:
            log.warning("Scheduled prompt skipped: no state/dir for %s:%s", chat_id, thread_id)
            return
        send_fn = self._make_send_fn(chat_id, thread_id)
        try:
            session = await self._session_mgr.get_or_start(state, send_fn)
            await send_fn(f"⏰ 定时任务触发：{prompt}")
            await session.send(prompt)
        except Exception as e:
            log.error("Scheduled prompt failed for %s:%s: %s", chat_id, thread_id, e)

    async def _cmd_setdir(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """直接设置工作目录：/setdir /path/to/dir"""
        if not _is_allowed(update):
            return
        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(update, "⚠️ 请在 Topic 内使用此命令。")
            return
        args = ctx.args
        if not args:
            await _safe_reply(update, "用法：`/setdir /path/to/dir`", parse_mode="Markdown")
            return
        path_str = " ".join(args)
        ok, resolved = validate_directory(path_str)
        if not ok:
            await _safe_reply(update, f"❌ {resolved}")
            return
        chat_id = update.effective_chat.id
        state = self._state_store.get_or_create(chat_id, thread_id)
        old_session = self._session_mgr.get_session(state.key())
        if old_session:
            await old_session.stop()
        state.working_directory = resolved
        state.pending_dir_input = False
        self._state_store.update(state)
        await _safe_reply(update, f"✅ 工作目录已设置：`{resolved}`\n正在启动 Claude…", parse_mode="Markdown")
        await self._start_claude_for_state(update, state)

    # ── Debug handler (temporary) ─────────────────────────────────────────────

    async def _on_debug(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            log.info("[DEBUG] update with no message: %s", update)
            return
        log.info(
            "[DEBUG] msg type: photo=%s doc=%s text=%s sticker=%s video=%s "
            "chat_type=%s thread_id=%s",
            bool(msg.photo), bool(msg.document), bool(msg.text),
            bool(msg.sticker), bool(msg.video),
            update.effective_chat.type if update.effective_chat else "?",
            msg.message_thread_id,
        )
        if msg.document:
            log.info("[DEBUG] doc mime=%s name=%s", msg.document.mime_type, msg.document.file_name)

    # ── Image handler ─────────────────────────────────────────────────────────

    async def _on_image(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Download photo/image document into working dir, then ask Claude to read it."""
        msg = update.effective_message
        chat_id = update.effective_chat.id if update.effective_chat else "?"
        thread_id = _get_thread_id(update)
        log.info("[%s:%s] _on_image triggered photo=%s doc=%s",
                 chat_id, thread_id,
                 bool(msg and msg.photo),
                 bool(msg and msg.document))

        if not _is_allowed(update):
            log.warning("[%s:%s] _on_image rejected by _is_allowed", chat_id, thread_id)
            return
        if not thread_id:
            log.warning("[%s:%s] _on_image: no thread_id, replying with hint", chat_id, thread_id)
            await _safe_reply(update, "⚠️ 请在 Topic（话题）内发送图片。")
            return

        caption = msg.caption or ""
        log.info("[%s:%s] image received (caption=%r)", chat_id, thread_id, caption)

        # Get the file object (photo or document or sticker
        if msg.photo:
            file_obj = msg.photo[-1]  # largest size
            filename = f"image_{int(time.time())}.jpg"
        elif msg.sticker:
            file_obj = msg.sticker
            filename = f"sticker_{int(time.time())}.webp"
        elif msg.document:
            file_obj = msg.document
            filename = msg.document.file_name or f"image_{int(time.time())}.jpg"
        else:
            log.warning("[%s:%s] _on_image: no photo or document found", chat_id, thread_id)
            return

        state = self._state_store.get_or_create(chat_id, thread_id)
        if not state.working_directory:
            await self._prompt_dir(update, state)
            return

        send_fn = self._make_send_fn(chat_id, thread_id)
        try:
            session = await self._session_mgr.get_or_start(state, send_fn)
        except Exception:
            return

        # Download image to temp file, send base64 to Claude, then delete
        img_path = Path(state.working_directory) / filename
        try:
            tg_file = await ctx.bot.get_file(file_obj.file_id)
            await tg_file.download_to_drive(str(img_path))
            log.info("[%s:%s] image saved to %s", chat_id, thread_id, img_path)
        except Exception as e:
            log.error("[%s:%s] image download failed: %s", chat_id, thread_id, e)
            await _safe_reply(update, f"❌ 图片下载失败：{e}")
            return

        task_start = time.time()
        try:
            await session.send_image(str(img_path), caption)
            log.info("[%s:%s] image prompt sent to claude", chat_id, thread_id)
        except Exception as e:
            log.error("[%s:%s] send image prompt failed: %s", chat_id, thread_id, e)
            await _safe_reply(update, f"❌ 发送失败：{e}")
        finally:
            # Clean up image file after sending
            try:
                img_path.unlink()
            except OSError:
                pass

        if state.working_directory:
            await self._send_new_files(chat_id, thread_id, state.working_directory, task_start)

    # ── Callback: directory selection ─────────────────────────────────────────

    async def _cb_dir_select(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if not _is_allowed(update):
            return

        data = query.data  # "dir:~" or "dir:__manual__"
        if not data.startswith("dir:"):
            return

        thread_id = query.message.message_thread_id
        if not thread_id:
            await query.edit_message_text("⚠️ 请在 Topic 内使用。")
            return

        chat_id = update.effective_chat.id
        state = self._state_store.get_or_create(chat_id, thread_id)
        path_choice = data[4:]

        if path_choice == "__manual__":
            state.pending_dir_input = True
            state.status = "waiting_dir"
            self._state_store.update(state)
            await query.edit_message_text("✏️ 请发送绝对路径（例如 /Users/yourname/projects）：")
            return

        ok, resolved = validate_directory(path_choice)
        if not ok:
            await query.edit_message_text(f"❌ {resolved}\n请重新选择：", reply_markup=self._dir_keyboard())
            return

        state.working_directory = resolved
        state.pending_dir_input = False
        self._state_store.update(state)
        await query.edit_message_text(f"✅ 已选择目录：`{resolved}`\n正在启动 Claude…", parse_mode="Markdown")

        send_fn = self._make_send_fn(chat_id, thread_id)
        try:
            await self._session_mgr.get_or_start(state, send_fn)
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=f"🚀 Claude 会话已就绪！\n📂 工作目录：`{resolved}`\n\n直接发消息开始对话。",
                message_thread_id=thread_id,
                parse_mode="Markdown",
            )
        except Exception as e:
            log.error("[%s] start after dir select failed: %s", state.key(), e)

    # ── Plain text handler ────────────────────────────────────────────────────

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update):
            return

        thread_id = _get_thread_id(update)
        if not thread_id:
            await _safe_reply(
                update,
                "⚠️ 请在群组的 Topic（话题）内发消息，不支持主聊天界面。",
            )
            return

        chat_id = update.effective_chat.id
        text = update.effective_message.text or ""
        log.info("[%s:%s] user message: %d chars", chat_id, thread_id, len(text))

        state = self._state_store.get_or_create(chat_id, thread_id)

        # ── Waiting for manual directory input ────────────────────────────────
        if state.pending_dir_input:
            ok, resolved = validate_directory(text)
            if not ok:
                await _safe_reply(update, f"❌ {resolved}\n请重新输入绝对路径：")
                return
            state.working_directory = resolved
            state.pending_dir_input = False
            self._state_store.update(state)
            await _safe_reply(update, f"✅ 目录已设置：`{resolved}`\n正在启动 Claude…", parse_mode="Markdown")
            await self._start_claude_for_state(update, state)
            return

        # ── No directory set yet ──────────────────────────────────────────────
        if not state.working_directory:
            await self._prompt_dir(update, state)
            return

        # ── Ensure session is running ─────────────────────────────────────────
        send_fn = self._make_send_fn(chat_id, thread_id)
        try:
            session = await self._session_mgr.get_or_start(state, send_fn)
        except Exception:
            return  # error already sent to Telegram inside get_or_start

        # ── Forward to Claude ─────────────────────────────────────────────────
        task_start = time.time()
        try:
            await session.send(text)
            log.info("[%s:%s] forwarded to claude stdin", chat_id, thread_id)
        except Exception as e:
            log.error("[%s:%s] send to claude failed: %s", chat_id, thread_id, e)
            await _safe_reply(update, f"❌ 发送失败：{e}")
            return

        # ── Auto-send any new files Claude created ────────────────────────────
        if state.working_directory:
            await self._send_new_files(chat_id, thread_id, state.working_directory, task_start)
