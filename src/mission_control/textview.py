"""One-line text rendering of events, for terminals (replay.py, run_engine.py): the text-mode twin of the UI."""
from __future__ import annotations

from .events import Event, T

TICK = {"pending": "·", "running": "▶", "done": "✓", "verified": "✓✓", "failed": "✗", "skipped": "–"}


def format_event(e: Event, show_chunks: bool = True) -> str | None:
    d, a = e.data, e.agent_id or ""
    if e.type == T.RUN_STARTED:
        return f"■ RUN  {d['prompt']}"
    if e.type == T.RUN_FINISHED:
        return f"■ END  {d['status']}  {d.get('summary', '')}"
    if e.type in (T.RUN_PAUSED, T.RUN_RESUMED):
        return f"■ {e.type.split('.')[1].upper()}"
    if e.type == T.ORCH_STATUS:
        return f"◇ orchestrator: {d['text']}"
    if e.type in (T.PLAN_CREATED, T.PLAN_UPDATED):
        waves = " → ".join("{" + ",".join(w) + "}" for w in d["stages"])
        extra = f"  [{d['reason']}] +{d['added']} ~{d['changed']} -{d['removed']}" if e.type == T.PLAN_UPDATED else ""
        return f"▤ PLAN v{d['version']}  {waves}{extra}"
    if e.type == T.AGENT_CREATED:
        return f"+ agent {a} ({d['role']}, {d['model']}) tasks={d['task_ids']}"
    if e.type == T.AGENT_STATUS:
        return f"  agent {a}: {d['status']}"
    if e.type == T.TASK_STATUS:
        return f"  [{TICK[d['status']]:>2}] task {e.task_id}: {d['status']}"
    if e.type == T.AGENT_MESSAGE:
        who = "you" if d["role"] == "user" else a
        return f"  💬 {who}: {d['text']}"
    if e.type == T.FILE_CREATED:
        return f"  📄 {a} creates {d['path']}" + (" (rewrite)" if d.get("rewrite") else "")
    if e.type == T.FILE_CHUNK:
        return None if not show_chunks else "       " + d["text"].replace("\n", "⏎")
    if e.type == T.FILE_SAVED:
        return f"  💾 {d['path']} ({d['size']} B)"
    if e.type == T.DECISION:
        return f"  ⚑ decision {d['id']} ({d['source']}): {d['text']}"
    if e.type == T.USER_COMMAND:
        return f"  ⌨ user: {d['command']} → {d['target']}"
    if e.type == T.USAGE:
        return f"  Σ tokens in={d['tokens_in']} out={d['tokens_out']}"
    if e.type == T.LOG:
        level = d.get("level")
        if level == "debug":
            return None
        mark = {"error": "✖", "warn": "⚠", "info": "·"}.get(level, "·")
        return f"  {mark} [{d.get('source')}] {d.get('message')}"
    if e.type == T.APPROVAL_REQUESTED:
        return "⏸ waiting for your go-ahead (models: " + ", ".join(f"{k}={v}" for k, v in d["agents"].items()) + ")"
    if e.type == T.APPROVAL_GRANTED:
        return "▶ approved"
    if e.type == T.AGENT_UPDATED:
        return f"  agent {a} now uses {d['model']}"
    if e.type == T.ATTENTION_REQUIRED:
        lines = [f"❗ {e.task_id} needs your decision: {d['summary']}"]
        lines += [f"     reason: {r}" for r in d.get("reasons", [])]
        lines += [f"     option: {x}" for x in d.get("suggestions", [])]
        return "\n".join(lines)
    if e.type == T.ATTENTION_RESOLVED:
        return f"  decision for {e.task_id}: {d['decision']}" + (f" ({d['note']})" if d.get("note") else "")
    if e.type == T.ERROR:
        return f"  ⚠ ERROR {a}: {d['message']}"
    return f"  {e.type} {d}"
