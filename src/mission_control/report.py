"""A self-contained diagnostic report of a run, built from its event log.

Made to be handed over as-is ("something broke, here is the report"): environment, settings, every agent and its
model, how each task went, every warning and error with its evidence, the lead's diagnoses, the user's decisions,
and the tail of the full log. Large evidence is capped so the file stays readable."""
from __future__ import annotations

import platform
import sys
from datetime import datetime
from typing import Any, Iterable

from . import __version__
from .events import Event, T

DETAIL_CAP = 1500


def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ").strip() or "—"


def _when(ts: str) -> str:
    return ts[11:19] if len(ts) >= 19 else ts


def environment(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"app": f"Mission Control {__version__}", "python": platform.python_version(),
            "os": platform.platform(), "executable": sys.executable, **(extra or {})}


def build_report(events: Iterable[Event], env: dict[str, Any] | None = None) -> str:
    evs = list(events)
    started = next((e for e in evs if e.type == T.RUN_STARTED), None)
    finished = next((e for e in reversed(evs) if e.type == T.RUN_FINISHED), None)
    usage = next((e for e in reversed(evs) if e.type == T.USAGE), None)
    data = started.data if started else {}
    out: list[str] = [f"# Mission Control diagnostic report", "",
                      f"Generated {datetime.now().isoformat(timespec='seconds')}. Attach this file when you report a problem.", ""]

    out += ["## Environment", ""]
    for k, v in (env or environment()).items():
        out.append(f"- **{k}**: {v}")
    out.append("")

    out += ["## Run", ""]
    out.append(f"- **run id**: {evs[0].run_id if evs else '—'}")
    out.append(f"- **mode**: {'demo' if data.get('mock') else 'live'}" + (" (continuation of a previous run)" if data.get("resumed") else ""))
    out.append(f"- **project**: {data.get('project_path', '—')}")
    out.append(f"- **request**: {data.get('prompt', '—')}")
    for k, v in (data.get("settings") or {}).items():
        out.append(f"- **{k}**: {v}")
    if finished:
        out.append(f"- **outcome**: {finished.data.get('status')} — {finished.data.get('summary', '')}")
    else:
        out.append("- **outcome**: still running when the report was made")
    if started and evs:
        try:
            dur = datetime.fromisoformat(evs[-1].ts) - datetime.fromisoformat(started.ts)
            out.append(f"- **duration**: {int(dur.total_seconds() // 60)} min {int(dur.total_seconds() % 60)} s")
        except ValueError:
            pass
    if usage:
        out.append(f"- **tokens**: {usage.data['tokens_in']} in, {usage.data['tokens_out']} out")
    out.append("")

    # agents, tasks
    agents: dict[str, dict[str, Any]] = {}
    plan_tasks: dict[str, dict[str, Any]] = {}
    timeline: dict[str, list[str]] = {}
    failures: dict[str, list[str]] = {}
    for e in evs:
        if e.type == T.AGENT_CREATED and e.agent_id:
            agents[e.agent_id] = {"role": e.data.get("role"), "model": e.data.get("model"), "tasks": e.data.get("task_ids", []),
                                  "status": "queued"}
        elif e.type == T.AGENT_UPDATED and e.agent_id in agents:
            agents[e.agent_id]["model"] = e.data.get("model")
        elif e.type == T.AGENT_REMOVED and e.agent_id in agents:
            agents[e.agent_id]["status"] = "removed from the plan"
        elif e.type == T.AGENT_STATUS and e.agent_id in agents:
            agents[e.agent_id]["status"] = e.data.get("status")
        elif e.type in (T.PLAN_CREATED, T.PLAN_UPDATED):
            for t in e.data.get("tasks", []):
                plan_tasks[t["id"]] = t
        elif e.type == T.TASK_STATUS and e.task_id:
            seq = timeline.setdefault(e.task_id, [])
            if not seq or seq[-1] != e.data["status"]:
                seq.append(e.data["status"])
        elif e.type == T.ERROR and e.task_id:
            failures.setdefault(e.task_id, []).append(e.data.get("message", ""))
    if agents:
        out += ["## Agents", "", "| agent | role | model | last status |", "|---|---|---|---|"]
        for aid, a in agents.items():
            out.append(f"| {_cell(aid)} | {_cell(a['role'])} | {_cell(a['model'])} | {_cell(a['status'])} |")
        out.append("")
    if plan_tasks:
        out += ["## Tasks", "", "| task | title | status history | attempts failed | last problem |", "|---|---|---|---|---|"]
        for tid, t in plan_tasks.items():
            hist = timeline.get(tid, ["never started"])
            out.append(f"| {_cell(tid)} | {_cell(t.get('title'))} | {_cell(' → '.join(hist))} | {hist.count('failed')} | "
                       f"{_cell((failures.get(tid) or [''])[-1][:200])} |")
        out.append("")

    # the lead's reports to the user and the user's answers
    att = [e for e in evs if e.type in (T.ATTENTION_REQUIRED, T.ATTENTION_RESOLVED)]
    if att:
        out += ["## Decisions asked of the user", ""]
        for e in att:
            if e.type == T.ATTENTION_REQUIRED:
                out.append(f"- {_when(e.ts)} **{e.task_id}** needed a decision: {e.data.get('summary')}")
                for r in e.data.get("reasons", []):
                    out.append(f"  - reason: {r}")
                if e.data.get("diagnosis"):
                    out.append(f"  - lead's diagnosis: {e.data['diagnosis']}")
            else:
                out.append(f"- {_when(e.ts)} the user chose **{e.data.get('decision')}**" + (f": “{e.data['note']}”" if e.data.get("note") else ""))
        out.append("")

    changes = [e for e in evs if e.type == T.PLAN_UPDATED]
    if changes:
        out += ["## Plan changes", ""]
        for e in changes:
            out.append(f"- {_when(e.ts)} v{e.data.get('version')}: {e.data.get('reason')} (added {e.data.get('added')}, removed {e.data.get('removed')})")
        out.append("")

    problems = [e for e in evs if e.type == T.ERROR or (e.type == T.LOG and e.data.get("level") in ("warn", "error"))]
    out += ["## Errors and warnings", ""]
    if not problems:
        out += ["None.", ""]
    for e in problems:
        level = "error" if e.type == T.ERROR else e.data.get("level")
        where = " ".join(x for x in (e.task_id or "", e.agent_id or "") if x)
        source = e.data.get("source", "run")
        out.append(f"- {_when(e.ts)} **{level}** [{source}{(' ' + where) if where else ''}] {e.data.get('message', '')}")
        detail = e.data.get("detail")
        if detail:
            d = detail if len(detail) <= DETAIL_CAP else detail[:DETAIL_CAP] + f"\n…[{len(detail) - DETAIL_CAP} more characters]"
            out += ["", "  ```", *("  " + ln for ln in d.splitlines()), "  ```", ""]
    out.append("")

    decisions = [e for e in evs if e.type == T.DECISION]
    if decisions:
        out += ["## Decision ledger", ""]
        out += [f"- {e.data.get('id')} ({e.data.get('source')}): {e.data.get('text')}" for e in decisions]
        out.append("")

    logs = [e for e in evs if e.type == T.LOG][-60:]
    if logs:
        out += ["## Last log lines (all levels)", "", "```"]
        out += [f"{_when(e.ts)} {e.data.get('level', ''):5s} {e.data.get('source', ''):8s} {e.data.get('message', '')}" for e in logs]
        out += ["```", ""]
    return "\n".join(out)
