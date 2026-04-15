"""
SessionManager — owns all ClaudeSession instances, one per topic key.
"""
import asyncio
import time
from typing import Optional, Callable, Awaitable

from bot.claude_session import ClaudeSession
from bot.topic_state import TopicState, TopicStateStore
from bot.logger import get_logger

log = get_logger("telegram_bridge.session_manager")


class SessionManager:
    def __init__(self, state_store: TopicStateStore) -> None:
        self._store = state_store
        self._sessions: dict[str, ClaudeSession] = {}

    async def get_or_start(
        self,
        state: TopicState,
        send_fn: Callable[[str], Awaitable[None]],
    ) -> ClaudeSession:
        """Return existing running session or start a new one."""
        key = state.key()
        session = self._sessions.get(key)
        if session and session.is_running:
            return session
        # (Re)start
        session = await self._start_session(state, send_fn)
        return session

    async def _start_session(
        self,
        state: TopicState,
        send_fn: Callable[[str], Awaitable[None]],
    ) -> ClaudeSession:
        key = state.key()
        log.info("[%s] Creating new ClaudeSession in %s", key, state.working_directory)

        # on_exit is a no-op in the per-message architecture;
        # each send() call is self-contained.
        async def on_exit(sess: ClaudeSession) -> None:
            pass

        session = ClaudeSession(
            key=key,
            working_dir=state.working_directory,
            send_fn=send_fn,
            on_exit=on_exit,
        )
        try:
            await session.start()
        except Exception as e:
            err_msg = f"❌ 启动 Claude 失败：{e}\n请检查 CLAUDE_BIN 配置和 claude 命令是否可用。"
            log.error("[%s] %s", key, err_msg)
            try:
                await send_fn(err_msg)
            except Exception:
                pass
            raise

        self._sessions[key] = session
        state.status = "running"
        state.pid = session.pid
        self._store.update(state)
        return session

    async def restart(
        self,
        state: TopicState,
        send_fn: Callable[[str], Awaitable[None]],
    ) -> ClaudeSession:
        key = state.key()
        old = self._sessions.pop(key, None)
        if old:
            await old.stop()
        return await self._start_session(state, send_fn)

    async def stop_all(self) -> None:
        log.info("Stopping all %d sessions…", len(self._sessions))
        tasks = [s.stop() for s in self._sessions.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._sessions.clear()

    def get_session(self, key: str) -> Optional[ClaudeSession]:
        return self._sessions.get(key)
