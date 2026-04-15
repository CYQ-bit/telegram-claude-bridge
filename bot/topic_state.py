"""
TopicState — persistent state for each (chat_id, thread_id) pair.
Backed by data/topic_state.json with atomic writes.
"""
import json
import time
import tempfile
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from bot.config import STATE_FILE
from bot.logger import get_logger
from bot.utils import topic_key

log = get_logger("telegram_bridge.topic_state")


@dataclass
class TopicState:
    chat_id: int
    thread_id: int
    working_directory: Optional[str] = None
    pid: Optional[int] = None
    status: str = "idle"          # idle | waiting_dir | running | error
    last_activity: float = field(default_factory=time.time)
    pending_dir_input: bool = False   # True while waiting for manual path input

    def key(self) -> str:
        return topic_key(self.chat_id, self.thread_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TopicState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class TopicStateStore:
    """Thread-safe (asyncio-safe) in-memory store with JSON persistence."""

    def __init__(self) -> None:
        self._store: dict[str, TopicState] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self._store[k] = TopicState.from_dict(v)
            log.info("Loaded %d topic states from disk", len(self._store))
        except Exception as e:
            log.error("Failed to load topic_state.json: %s", e)

    def save(self) -> None:
        """Atomically write state to disk."""
        try:
            data = {k: v.to_dict() for k, v in self._store.items()}
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as e:
            log.error("Failed to save topic_state.json: %s", e)

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def get(self, chat_id: int, thread_id: int) -> Optional[TopicState]:
        return self._store.get(topic_key(chat_id, thread_id))

    def get_or_create(self, chat_id: int, thread_id: int) -> TopicState:
        key = topic_key(chat_id, thread_id)
        if key not in self._store:
            self._store[key] = TopicState(chat_id=chat_id, thread_id=thread_id)
            self.save()
        return self._store[key]

    def update(self, state: TopicState) -> None:
        state.last_activity = time.time()
        self._store[state.key()] = state
        self.save()

    def all_states(self) -> list[TopicState]:
        return list(self._store.values())

    def delete(self, chat_id: int, thread_id: int) -> None:
        key = topic_key(chat_id, thread_id)
        self._store.pop(key, None)
        self.save()
