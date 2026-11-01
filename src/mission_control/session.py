"""A run 'session': owns the live EventLog and reacts to user messages/commands.

Phase 1 ships `MockSession`: it replays the scripted mock run (src/mission_control/mock.py)
through the REAL EventLog at its original pace, so the UI, the SSE stream and the on-disk log
all behave exactly as they will with the real engine. User chat messages are recorded and get a
clearly-labelled canned reply. The real orchestrator (Phase 2) implements the same small interface.
"""
from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .eventlog import EventLog
from .events import COMMANDS, ORCHESTRATOR_ID, T, Event
from .mock import MODEL as MOCK_MODEL, build_mock_run
from .workspace import Workspace

MAX_MESSAGE_CHARS = 4000


class SessionError(ValueError):
    """User-facing problem. `status` is the HTTP status; `code` + `params` let the UI show it in the user's language
    (the English `message` is the fallback)."""

    def __init__(self, message: str, status: int = 400, code: str | None = None, params: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.params = params or {}


MOCK_STRINGS = {
    "en": {
        "reply": "(demo) Got your message: “{preview}”. In a live run this is where I would act on it and record the decision in the ledger.",
        "stopped_note": "The run was stopped.",
        "pause_note": "Demo mode: the command was recorded but has no effect on the pre-recorded run. Real pause/resume works in Live mode.",
        "status_pause": "(demo) “pause” command received", "status_resume": "(demo) “resume” command received",
        "empty": "The message is empty", "too_long": "The message is longer than {limit} characters",
        "no_agent": "No agent with id “{target}”", "bad_command": "Invalid command: {command}",
    },
    "fa": {
        "reply": "(نمونه) پیامت را گرفتم: «{preview}». در اجرای واقعی همین‌جا طبق آن عمل می‌کنم و تصمیم در دفترچه‌ی تصمیمات ثبت می‌شود.",
        "stopped_note": "اجرا متوقف شد.",
        "pause_note": "حالت نمونه: فرمان ثبت شد ولی روی اجرای از پیش ضبط‌شده اثری ندارد. توقف و ادامه‌ی واقعی در حالت زنده کار می‌کند.",
        "status_pause": "(نمونه) فرمان «توقف» دریافت شد", "status_resume": "(نمونه) فرمان «ادامه» دریافت شد",
        "empty": "پیام خالی است", "too_long": "پیام بیش از {limit} کاراکتر است",
        "no_agent": "ایجنتی با شناسه‌ی «{target}» وجود ندارد", "bad_command": "فرمان نامعتبر: {command}",
    },
}


def rotate_events_file(ws: Workspace) -> None:
    """A new run in the same folder starts a fresh events.jsonl; the old one is kept."""
    p = ws.events_path
    if p.exists() and p.stat().st_size > 0:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        p.rename(p.with_name(f"events.{stamp}.jsonl"))


class MockSession:
    mode = "demo"

    def __init__(self, ws: Workspace, prompt: str, *, speed: float = 2.0, run_id: str | None = None, lang: str = "en",
                 require_approval: bool = False) -> None:
        self.ws = ws
        self.prompt = prompt
        self.speed = speed
        self.lang = lang if lang in MOCK_STRINGS else "en"
        self.run_id = run_id or "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        rotate_events_file(ws)
        self.log = EventLog(ws.events_path, self.run_id)
        self._agents = {ORCHESTRATOR_ID}
        self.log.add_listener(self._track_agents)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ended = False
        self.require_approval = require_approval
        self._awaiting = False
        self._approved: dict[str, str] | None = None
        self._approval_event = threading.Event()
        self.can_continue = False
        self._thread = threading.Thread(target=self._play, name="mock-player", daemon=True)

    def _s(self, key: str, **kw: Any) -> str:
        return MOCK_STRINGS[self.lang][key].format(**kw)

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        return not self._ended

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def stop(self) -> None:
        """Stop playback and record the end of the run (idempotent)."""
        self._stop.set()
        self._end("stopped", self._s("stopped_note"))

    def _end(self, status: str, summary: str = "") -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
            data: dict[str, Any] = {"status": status}
            if summary:
                data["summary"] = summary
            self.log.append(T.RUN_FINISHED, data)

    def _track_agents(self, ev: Event) -> None:
        if ev.type == T.AGENT_CREATED and ev.agent_id:
            self._agents.add(ev.agent_id)

    # ------------------------------------------------------------------- player
    def _play(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = build_mock_run(Path(tmp) / "script.jsonl", lang=self.lang)
        prev: datetime | None = None
        inputs = sorted(p.name for p in self.ws.inputs_dir.glob("*")) if self.ws.inputs_dir.exists() else []
        for ev in script:
            cur = datetime.fromisoformat(ev.ts)
            if prev is not None and self.speed > 0:
                gap = min((cur - prev).total_seconds(), 1.5) / self.speed
                if gap > 0 and self._stop.wait(gap):
                    return
            prev = cur
            if self._stop.is_set():
                return
            data = dict(ev.data)
            if ev.type == T.RUN_STARTED:
                data.update(project_path=str(self.ws.root), prompt=self.prompt, mock=True,
                            inputs=[f"inputs/{n}" for n in inputs])
            if ev.type == T.APPROVAL_REQUESTED and self.require_approval:
                with self._lock:
                    if self._ended:
                        return
                    self._awaiting = True
                    self.log.append(ev.type, data)
                while not self._approval_event.wait(0.2):     # the demo waits for your go-ahead, like the engine
                    if self._stop.is_set():
                        return
                continue
            if ev.type == T.APPROVAL_GRANTED and self.require_approval:
                with self._lock:
                    if self._ended:
                        return
                    approved = self._approved or {}
                    for aid, m in approved.items():
                        if m != MOCK_MODEL:
                            self.log.append(T.AGENT_UPDATED, {"model": m}, agent_id=aid)
                    self.log.append(T.APPROVAL_GRANTED, {"models": {**data["models"], **approved}})
                    self._awaiting = False
                prev = None                                   # timing restarts after the pause for approval
                continue
            if ev.type == T.RUN_FINISHED:
                with self._lock:  # the scripted ending also marks the session as over
                    if self._ended:
                        return
                    self._ended = True
                    self.log.append(ev.type, data, agent_id=ev.agent_id, task_id=ev.task_id)
                return
            with self._lock:
                if self._ended:
                    return
                self.log.append(ev.type, data, agent_id=ev.agent_id, task_id=ev.task_id)

    # -------------------------------------------------------------- user actions
    def post_message(self, target: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            raise SessionError(MOCK_STRINGS["en"]["empty"], code="message_empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise SessionError(MOCK_STRINGS["en"]["too_long"].format(limit=MAX_MESSAGE_CHARS), code="message_too_long", params={"limit": MAX_MESSAGE_CHARS})
        if target not in self._agents:
            raise SessionError(MOCK_STRINGS["en"]["no_agent"].format(target=target), 404, "no_agent", {"target": target})
        self.log.append(T.AGENT_MESSAGE, {"role": "user", "text": text}, agent_id=target)
        preview = text if len(text) <= 40 else text[:40] + "…"
        reply = self._s("reply", preview=preview)
        timer = threading.Timer(0.8, self._append_quietly, args=(T.AGENT_MESSAGE, {"role": "agent", "text": reply}, target))
        timer.daemon = True
        timer.start()

    def _append_quietly(self, type_: str, data: dict[str, Any], agent_id: str) -> None:
        """For deferred replies: if the project folder vanished meanwhile, drop the reply silently."""
        try:
            self.log.append(type_, data, agent_id=agent_id)
        except OSError:
            pass

    def post_approval(self, models: dict[str, str] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._ended:
                raise SessionError("This run has finished", 409, "run_finished")
            if not self._awaiting:
                raise SessionError("There is no plan waiting for approval", 409, "not_awaiting")
            models = {k: str(v).strip() for k, v in (models or {}).items()}
            for aid, m in models.items():
                if aid not in self._agents or aid == ORCHESTRATOR_ID:
                    raise SessionError(f"No agent with id “{aid}”", 404, "no_agent", {"target": aid})
                if not m:
                    raise SessionError("Model name is empty", 400, "model_missing", {"model": m, "installed": []})
            self._approved = models
        self._approval_event.set()
        return {"applied": True}

    def post_resolution(self, task_id: str, decision: str, note: str = "") -> dict[str, Any]:
        raise SessionError("That task is not waiting for a decision", 409, "no_attention")

    def post_command(self, command: str, target: str = "all") -> dict[str, Any]:
        if command not in COMMANDS:
            raise SessionError(MOCK_STRINGS["en"]["bad_command"].format(command=command), code="bad_command", params={"command": command})
        if target != "all" and target not in self._agents:
            raise SessionError(MOCK_STRINGS["en"]["no_agent"].format(target=target), 404, "no_agent", {"target": target})
        if command == "stop":
            self.log.append(T.USER_COMMAND, {"command": "stop", "target": target})
            self.stop()
            return {"applied": True, "note": ""}   # the run-ended notice already says it; one action, one message
        # pause/resume cannot change a pre-recorded replay; say so instead of pretending
        self.log.append(T.ORCH_STATUS, {"text": self._s("status_pause" if command == "pause" else "status_resume")})
        return {"applied": False, "note": self._s("pause_note")}
