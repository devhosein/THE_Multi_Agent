"""The event contract between the engine and the UI.

Everything the user sees on screen is a pure function of this event stream.
That is what lets us build the UI against a mock run (no tokens spent) and later
plug the real engine in without touching the UI.

Zero dependencies on purpose: plain dataclasses + json.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
ORCHESTRATOR_ID = "orchestrator"  # reserved agent_id for the lead agent's chat stream

TASK_STATUSES = ("pending", "running", "done", "verified", "failed", "skipped")
AGENT_STATUSES = ("queued", "running", "paused", "done", "error")
RUN_END_STATUSES = ("completed", "failed", "stopped")
COMMANDS = ("pause", "resume", "stop")
MESSAGE_ROLES = ("user", "agent")
DECISION_SOURCES = ("user", "orchestrator", "agent")
LOG_LEVELS = ("debug", "info", "warn", "error")
RESOLUTIONS = ("retry", "skip", "stop")

# "done" = the agent says it finished; "verified" = an independent check agreed.
TASK_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "skipped"},
    "running": {"done", "failed", "pending"},
    "done": {"verified", "running", "failed"},
    "failed": {"running", "skipped"},
    "verified": {"running"},  # re-opened by a replan
    "skipped": {"pending"},
}
AGENT_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running"},
    "running": {"paused", "done", "error"},
    "paused": {"running"},
    "error": {"running"},
    "done": {"running"},
}


class T:
    """Event type names."""

    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    ORCH_STATUS = "orchestrator.status"
    PLAN_CREATED = "plan.created"
    PLAN_UPDATED = "plan.updated"
    AGENT_CREATED = "agent.created"
    AGENT_REMOVED = "agent.removed"
    AGENT_STATUS = "agent.status"
    AGENT_MESSAGE = "agent.message"
    TASK_STATUS = "task.status"
    FILE_CREATED = "file.created"
    FILE_CHUNK = "file.chunk"
    FILE_SAVED = "file.saved"
    FILE_DELETED = "file.deleted"
    DECISION = "decision.recorded"
    USER_COMMAND = "user.command"
    USAGE = "usage.updated"
    ERROR = "error"
    LOG = "log"                                   # diagnostics for the Logs panel and the shareable report
    APPROVAL_REQUESTED = "approval.requested"     # the plan and team are ready; waiting for the user's go
    APPROVAL_GRANTED = "approval.granted"
    AGENT_UPDATED = "agent.updated"               # e.g. the user picked another model for this agent
    ATTENTION_REQUIRED = "attention.required"     # a task failed and the lead could not rescue it: the user decides
    ATTENTION_RESOLVED = "attention.resolved"


# type -> (required keys in data, needs envelope agent_id, needs envelope task_id)
SPEC: dict[str, tuple[tuple[str, ...], bool, bool]] = {
    T.RUN_STARTED: (("project_path", "prompt"), False, False),
    T.RUN_FINISHED: (("status",), False, False),
    T.RUN_PAUSED: ((), False, False),
    T.RUN_RESUMED: ((), False, False),
    T.ORCH_STATUS: (("text",), False, False),
    T.PLAN_CREATED: (("version", "tasks"), False, False),
    T.PLAN_UPDATED: (("version", "reason", "tasks"), False, False),
    T.AGENT_CREATED: (("role", "model", "title", "task_ids"), True, False),
    T.AGENT_REMOVED: ((), True, False),
    T.AGENT_STATUS: (("status",), True, False),
    T.AGENT_MESSAGE: (("role", "text"), True, False),
    T.TASK_STATUS: (("status",), False, True),
    T.FILE_CREATED: (("path",), True, False),
    T.FILE_CHUNK: (("path", "text"), True, False),
    T.FILE_SAVED: (("path", "size"), True, False),
    T.FILE_DELETED: (("path",), True, False),
    T.DECISION: (("id", "text", "source"), False, False),
    T.USER_COMMAND: (("command", "target"), False, False),
    T.USAGE: (("tokens_in", "tokens_out"), False, False),
    T.ERROR: (("message",), False, False),
    T.LOG: (("level", "source", "message"), False, False),
    T.APPROVAL_REQUESTED: (("agents",), False, False),
    T.APPROVAL_GRANTED: (("models",), False, False),
    T.AGENT_UPDATED: (("model",), True, False),
    T.ATTENTION_REQUIRED: (("title", "summary", "reasons", "suggestions"), False, True),
    T.ATTENTION_RESOLVED: (("decision",), False, True),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EventError(ValueError):
    """Raised when an event or an event stream violates the contract."""


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    run_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    agent_id: str | None = None
    task_id: str | None = None
    v: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "v": self.v, "seq": self.seq, "ts": self.ts, "run_id": self.run_id, "type": self.type,
        }
        if self.agent_id is not None:
            d["agent_id"] = self.agent_id
        if self.task_id is not None:
            d["task_id"] = self.task_id
        d["data"] = self.data
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        try:
            return cls(
                seq=int(d["seq"]), ts=str(d["ts"]), run_id=str(d["run_id"]), type=str(d["type"]),
                data=dict(d.get("data") or {}), agent_id=d.get("agent_id"), task_id=d.get("task_id"),
                v=int(d.get("v", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise EventError(f"malformed event: {e!r}") from e

    @classmethod
    def from_json(cls, line: str) -> "Event":
        try:
            return cls.from_dict(json.loads(line))
        except json.JSONDecodeError as e:
            raise EventError(f"invalid JSON: {e}") from e

    def validate(self) -> list[str]:
        return validate_event_fields(self.type, self.data, self.agent_id, self.task_id)


def validate_event_fields(type_: str, data: Any, agent_id: str | None, task_id: str | None) -> list[str]:
    spec = SPEC.get(type_)
    if spec is None:
        return [f"unknown event type: {type_!r}"]
    if not isinstance(data, dict):
        return ["data must be an object"]
    required, needs_agent, needs_task = spec
    errors = [f"{type_}: missing data.{k}" for k in required if k not in data]
    if needs_agent and not agent_id:
        errors.append(f"{type_}: agent_id is required")
    if needs_task and not task_id:
        errors.append(f"{type_}: task_id is required")

    def enum(key: str, allowed: tuple[str, ...]) -> None:
        if key in data and data[key] not in allowed:
            errors.append(f"{type_}: data.{key}={data[key]!r} not in {allowed}")

    if type_ == T.TASK_STATUS:
        enum("status", TASK_STATUSES)
    elif type_ == T.AGENT_STATUS:
        enum("status", AGENT_STATUSES)
    elif type_ == T.RUN_FINISHED:
        enum("status", RUN_END_STATUSES)
    elif type_ == T.USER_COMMAND:
        enum("command", COMMANDS)
    elif type_ == T.AGENT_MESSAGE:
        enum("role", MESSAGE_ROLES)
    elif type_ == T.DECISION:
        enum("source", DECISION_SOURCES)
    elif type_ == T.LOG:
        enum("level", LOG_LEVELS)
    elif type_ == T.ATTENTION_RESOLVED:
        enum("decision", RESOLUTIONS)
    return errors


def check_stream(events: list[Event]) -> list[str]:
    """Whole-stream consistency: monotonic seq, known agents/tasks, legal status transitions."""
    errors: list[str] = []
    agents: set[str] = {ORCHESTRATOR_ID}
    agent_status: dict[str, str] = {}
    task_ids: set[str] = set()
    task_status: dict[str, str] = {}
    awaiting_approval = False
    open_attention: set[str] = set()
    prev_seq = 0
    for ev in events:
        where = f"seq {ev.seq} ({ev.type})"
        if ev.seq != prev_seq + 1:
            errors.append(f"{where}: seq should be {prev_seq + 1}")
        prev_seq = ev.seq
        errors += [f"{where}: {e}" for e in ev.validate()]

        if ev.type in (T.PLAN_CREATED, T.PLAN_UPDATED):
            for t in ev.data.get("tasks", []):
                tid = t.get("id")
                if tid not in task_ids:
                    task_ids.add(tid)
                    task_status[tid] = "pending"
        if ev.type == T.AGENT_REMOVED and ev.agent_id:
            if ev.agent_id not in agents or ev.agent_id == ORCHESTRATOR_ID:
                errors.append(f"{where}: cannot remove unknown agent {ev.agent_id!r}")
            agents.discard(ev.agent_id)
            agent_status.pop(ev.agent_id, None)
            continue
        if ev.type == T.AGENT_CREATED and ev.agent_id:
            agents.add(ev.agent_id)
            agent_status[ev.agent_id] = "queued"
            for tid in ev.data.get("task_ids", []):
                if tid not in task_ids:
                    errors.append(f"{where}: agent references unknown task {tid!r}")
        elif ev.agent_id and ev.agent_id not in agents:
            errors.append(f"{where}: unknown agent {ev.agent_id!r}")

        if ev.type == T.APPROVAL_REQUESTED:
            awaiting_approval = True
        elif ev.type == T.APPROVAL_GRANTED:
            if not awaiting_approval:
                errors.append(f"{where}: approval granted but never requested")
            awaiting_approval = False
        elif ev.type == T.ATTENTION_REQUIRED:
            open_attention.add(ev.task_id)
        elif ev.type == T.ATTENTION_RESOLVED:
            if ev.task_id not in open_attention:
                errors.append(f"{where}: attention resolved for {ev.task_id!r} but none was required")
            open_attention.discard(ev.task_id)
        if ev.type == T.AGENT_STATUS and ev.agent_id in agent_status:
            old, new = agent_status[ev.agent_id], ev.data.get("status")
            if new not in AGENT_TRANSITIONS.get(old, set()):
                errors.append(f"{where}: agent {ev.agent_id} illegal transition {old}->{new}")
            agent_status[ev.agent_id] = new
        if ev.type == T.TASK_STATUS:
            tid = ev.task_id
            if tid not in task_ids:
                errors.append(f"{where}: unknown task {tid!r}")
            else:
                old, new = task_status[tid], ev.data.get("status")
                if new not in TASK_TRANSITIONS.get(old, set()):
                    errors.append(f"{where}: task {tid} illegal transition {old}->{new}")
                task_status[tid] = new
    return errors
