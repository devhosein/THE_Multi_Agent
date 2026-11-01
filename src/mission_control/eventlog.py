"""Append-only JSONL event log.

One file per run (`.mission/events.jsonl`). It is simultaneously:
  * the live feed for the UI (via listeners -> WebSocket in Phase 1),
  * the audit trail,
  * the replay source (reconnecting UIs ask for `after_seq=N`),
  * the future basis for resuming/forking a run.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .events import Event, EventError, now_iso


def read_events(path: str | Path, after_seq: int = 0) -> list[Event]:
    """Read events with seq > after_seq. A torn *last* line (crash mid-write) is ignored;
    corruption anywhere else raises EventError."""
    p = Path(path)
    if not p.exists():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8").split("\n") if ln.strip()]
    out: list[Event] = []
    for i, line in enumerate(lines):
        try:
            ev = Event.from_json(line)
        except EventError:
            if i == len(lines) - 1:
                break
            raise
        if ev.seq > after_seq:
            out.append(ev)
    return out


def _repair_torn_tail(path: Path) -> None:
    """If the file does not end with a newline, drop the torn partial line."""
    if not path.exists() or path.stat().st_size == 0:
        return
    raw = path.read_bytes()
    if raw.endswith(b"\n"):
        return
    cut = raw.rfind(b"\n") + 1  # 0 if there is no newline at all
    path.write_bytes(raw[:cut])


class EventLog:
    def __init__(
        self,
        path: str | Path,
        run_id: str,
        *,
        validate: bool = True,
        clock: Callable[[], str] = now_iso,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._validate = validate
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Event], None]] = []
        _repair_torn_tail(self.path)
        existing = read_events(self.path)
        self._seq = existing[-1].seq if existing else 0

    @property
    def last_seq(self) -> int:
        return self._seq

    def add_listener(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Listeners run synchronously inside append (keeps ordering) - they must be fast,
        e.g. push onto a queue. Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def append(
        self,
        type: str,
        data: dict[str, Any] | None = None,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        ts: str | None = None,
    ) -> Event:
        with self._lock:
            ev = Event(
                seq=self._seq + 1, ts=ts or self._clock(), run_id=self.run_id, type=type,
                data=data or {}, agent_id=agent_id, task_id=task_id,
            )
            if self._validate:
                errors = ev.validate()
                if errors:
                    raise EventError("; ".join(errors))
            with self.path.open("a", encoding="utf-8", newline="\n") as f:  # LF on Windows too
                f.write(ev.to_json() + "\n")
            self._seq = ev.seq
            for cb in list(self._listeners):
                try:
                    cb(ev)
                except Exception:  # a broken UI connection must never break the engine
                    pass
            return ev

    def read(self, after_seq: int = 0) -> list[Event]:
        return read_events(self.path, after_seq)


def replay(
    events: Iterable[Event],
    speed: float = 1.0,
    *,
    max_gap: float = 1.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[Event]:
    """Yield events with the original relative timing. speed=0 means 'no waiting'.
    Gaps are capped at max_gap seconds (before dividing by speed)."""
    from datetime import datetime

    prev: datetime | None = None
    for ev in events:
        cur = datetime.fromisoformat(ev.ts)
        if prev is not None and speed > 0:
            gap = min(max((cur - prev).total_seconds(), 0.0), max_gap)
            if gap > 0:
                sleep(gap / speed)
        prev = cur
        yield ev
