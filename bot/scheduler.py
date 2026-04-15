"""
Scheduler — cron-based task runner for Telegram topics.

Stores schedules in data/schedules.json.
Each schedule: { id, chat_id, thread_id, cron, prompt, enabled, created_at }

Commands:
  /schedule add <cron> <prompt>   — add a new schedule
  /schedule list                  — list schedules for this topic
  /schedule del <id>              — delete a schedule
  /schedule on <id>               — enable
  /schedule off <id>              — disable
"""
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Awaitable, Optional

from bot.config import DATA_DIR
from bot.logger import get_logger

log = get_logger("telegram_bridge.scheduler")

SCHEDULES_FILE = DATA_DIR / "schedules.json"

# Minimal cron parser: supports "M H DOM MON DOW" with * and */n
# For simplicity we use a polling loop (check every minute).


@dataclass
class Schedule:
    id: str
    chat_id: int
    thread_id: int
    cron: str          # "M H DOM MON DOW"
    prompt: str
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def key(self) -> str:
        return f"{self.chat_id}:{self.thread_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Schedule":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _cron_matches(cron: str, t: time.struct_time) -> bool:
    """Return True if the cron expression matches the given time (minute resolution)."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts

    def _match(field: str, value: int, min_v: int, max_v: int) -> bool:
        if field == "*":
            return True
        if field.startswith("*/"):
            step = int(field[2:])
            return value % step == 0
        if "," in field:
            return value in {int(x) for x in field.split(",")}
        if "-" in field:
            lo, hi = field.split("-")
            return int(lo) <= value <= int(hi)
        return int(field) == value

    return (
        _match(minute, t.tm_min, 0, 59)
        and _match(hour, t.tm_hour, 0, 23)
        and _match(dom, t.tm_mday, 1, 31)
        and _match(month, t.tm_mon, 1, 12)
        and _match(dow, t.tm_wday, 0, 6)  # 0=Monday in Python, but cron 0=Sunday
    )


class ScheduleStore:
    def __init__(self) -> None:
        self._schedules: dict[str, Schedule] = {}
        self._load()

    def _load(self) -> None:
        if not SCHEDULES_FILE.exists():
            return
        try:
            raw = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
            for item in raw:
                s = Schedule.from_dict(item)
                self._schedules[s.id] = s
            log.info("Loaded %d schedules from disk", len(self._schedules))
        except Exception as e:
            log.error("Failed to load schedules.json: %s", e)

    def _save(self) -> None:
        try:
            data = [s.to_dict() for s in self._schedules.values()]
            tmp = SCHEDULES_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(SCHEDULES_FILE)
        except Exception as e:
            log.error("Failed to save schedules.json: %s", e)

    def add(self, chat_id: int, thread_id: int, cron: str, prompt: str) -> Schedule:
        s = Schedule(
            id=uuid.uuid4().hex[:8],
            chat_id=chat_id,
            thread_id=thread_id,
            cron=cron,
            prompt=prompt,
        )
        self._schedules[s.id] = s
        self._save()
        return s

    def delete(self, schedule_id: str) -> bool:
        if schedule_id in self._schedules:
            del self._schedules[schedule_id]
            self._save()
            return True
        return False

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        s = self._schedules.get(schedule_id)
        if s:
            s.enabled = enabled
            self._save()
            return True
        return False

    def list_for_topic(self, chat_id: int, thread_id: int) -> list[Schedule]:
        return [s for s in self._schedules.values()
                if s.chat_id == chat_id and s.thread_id == thread_id]

    def get(self, schedule_id: str) -> Optional[Schedule]:
        return self._schedules.get(schedule_id)

    def all_enabled(self) -> list[Schedule]:
        return [s for s in self._schedules.values() if s.enabled]


class Scheduler:
    """Runs a background loop that fires scheduled prompts."""

    def __init__(self, store: ScheduleStore) -> None:
        self._store = store
        self._send_fns: dict[str, Callable[[str], Awaitable[None]]] = {}
        self._run_fn: Optional[Callable[[int, int, str], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None

    def register_send(self, chat_id: int, thread_id: int,
                      send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Register a send function for a topic (called when session starts)."""
        key = f"{chat_id}:{thread_id}"
        self._send_fns[key] = send_fn

    def set_run_fn(self, fn: Callable[[int, int, str], Awaitable[None]]) -> None:
        """Set the function that actually sends a prompt to Claude for a topic."""
        self._run_fn = fn

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        log.info("Scheduler started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        """Check every 60 seconds and fire matching schedules."""
        # Align to next minute boundary
        now = time.time()
        await asyncio.sleep(60 - (now % 60))

        while True:
            t = time.localtime()
            for s in self._store.all_enabled():
                if _cron_matches(s.cron, t):
                    log.info("Firing schedule %s for %s:%s", s.id, s.chat_id, s.thread_id)
                    asyncio.create_task(self._fire(s))
            await asyncio.sleep(60)

    async def _fire(self, s: Schedule) -> None:
        if self._run_fn:
            try:
                await self._run_fn(s.chat_id, s.thread_id, s.prompt)
            except Exception as e:
                log.error("Schedule %s fire failed: %s", s.id, e)
