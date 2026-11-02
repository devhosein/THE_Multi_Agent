#!/usr/bin/env python3
r"""Single-agent BASELINE: one agent builds the whole project, no lead, no plan, no team.

    python scripts/solo_agent.py --project C:\tmp\cafe-solo --prompt-file eval\tasks\cafe.prompt.txt --model ornith-1.5:35b

It is Mission Control minus the orchestration: the SAME worker loop, tools, sandbox, mechanical checks, retries on
failed checks and strength profile, but a single task that owns everything. Comparing it with a Mission Control run
on the same prompt and model isolates what planning, contracts, specialist agents and reviews are worth.

    --check   also give it Mission Control's project check (the fair comparison): the check_project tool and the
              integration check after each attempt, with the browser check when Playwright is installed. Problems go
              back to the agent exactly as in Mission Control (cosmetic ones only until the last attempt).
Writes .mission/events.jsonl (readable with scripts/replay.py) and .mission/solo_result.json (time, tokens, calls)."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_control import utf8_console  # noqa: E402
from mission_control.checks import check_task  # noqa: E402
from mission_control.eventlog import EventLog  # noqa: E402
from mission_control.integration import check_integration  # noqa: E402
from mission_control.llm import DEFAULT_MODEL, LLMError, OllamaClient  # noqa: E402
from mission_control.orchestrator import build_worker_system, build_worker_user  # noqa: E402
from mission_control.paths import clean_path_input  # noqa: E402
from mission_control.plan import Plan, Task  # noqa: E402
from mission_control.profiles import resolve  # noqa: E402
from mission_control.session import rotate_events_file  # noqa: E402
from mission_control.tools import ToolBox  # noqa: E402
from mission_control.worker import WorkerLimits, run_worker  # noqa: E402
from mission_control.workspace import init_workspace  # noqa: E402

utf8_console()

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--project", required=True, help="project folder (created if missing)")
src = ap.add_mutually_exclusive_group(required=True)
src.add_argument("--prompt")
src.add_argument("--prompt-file", help="UTF-8 text file with the prompt")
ap.add_argument("--model", default=DEFAULT_MODEL)
ap.add_argument("--host", default=None, help="Ollama URL, default http://localhost:11434")
ap.add_argument("--strength", choices=("auto", "small", "medium", "large"), default="auto")
ap.add_argument("--check", action="store_true", help="give the agent Mission Control's project check (fair comparison)")
args = ap.parse_args()

prompt = args.prompt if args.prompt else Path(args.prompt_file).read_text(encoding="utf-8").strip()
llm = OllamaClient(args.model, args.host)
try:
    info = {m["name"]: m.get("parameter_size", "") for m in llm.list_models_detailed()}
except LLMError as e:
    sys.exit(f"Cannot reach Ollama: {e}")
if args.model not in info and f"{args.model}:latest" not in info:
    sys.exit(f"Model {args.model!r} is not installed. Run: ollama pull {args.model}")
prof = resolve(args.strength, info.get(args.model, info.get(f"{args.model}:latest", "")), args.model)

ws = init_workspace(clean_path_input(args.project))
rotate_events_file(ws)
run_id = "solo_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
log = EventLog(ws.events_path, run_id)
log.append("run.started", {"project_path": str(ws.root), "prompt": prompt, "mock": False, "model": args.model,
                           "settings": {"mode": "single-agent baseline" + (" + project check" if args.check else ""),
                                        "profile": prof.name}})

# One task that owns the whole project. Its acceptance criteria restate the prompt, nothing more (a Mission Control
# plan writes its own, more detailed ones: that is part of what is being compared).
task = Task(id="solo", title="Build the whole project", role="developer", goal=prompt, owns=("**",),
            acceptance=("Everything the request asks for is implemented and works together.",
                        "Files are complete: no placeholders, no TODOs."))
plan = Plan(version=1, tasks=(task,))
usage = {"in": 0, "out": 0, "calls": 0}


def on_usage(tin: int, tout: int) -> None:
    usage["in"] += tin
    usage["out"] += tout
    usage["calls"] += 1


def say(text: str) -> None:
    print(f"  agent: {text[:300]}", flush=True)
    log.append("agent.message", {"role": "agent", "text": text[:4000]}, agent_id="a_solo")


def note(level: str, source: str, message: str, detail: str | None = None) -> None:
    if level != "debug":
        print(f"  [{level}] {source}: {message}", flush=True)
    log.append("log", {"level": level, "source": source, "message": message, **({"detail": detail[:4000]} if detail else {})})


def run_check() -> str:
    """check_project, as Mission Control serves it to its agents."""
    report = check_integration(ws.root)
    note("info", "integration", "The agent ran the project check: " + report.summary())
    return report.as_text()


print(f"project: {ws.root}\nmodel:   {args.model} (profile {prof.name}){'  + project check' if args.check else ''}\n")
t0 = time.monotonic()
written: list[str] = []
feedback: list[str] | None = None
outcome, problems, summary = "failed", [], ""
for attempt in range(1, prof.max_attempts + 1):
    print(f"attempt {attempt}/{prof.max_attempts}", flush=True)
    box = ToolBox(ws, task.owns, log, "a_solo", chunk_delay_cap=0, read_chars=prof.read_chars,
                  run_check=run_check if args.check else None)
    box.written = list(written)
    result = run_worker(
        llm, box, build_worker_system(task, plan, ws, "", prof), build_worker_user(feedback, box.written, []),
        gate=lambda: None, deliver=lambda: [], reply=lambda msgs, text: None, on_message=say, on_usage=on_usage,
        log=note, limits=WorkerLimits(max_steps=prof.max_steps, temperature=prof.temperature))
    written = box.written
    problems = check_task(ws, task, written) if result.ok else [result.error or "the agent failed"]
    if not problems and args.check:                 # the same rule as EngineSession._attempts
        report = check_integration(ws.root)
        note("info" if not report.problems else "warn", "integration", report.summary(),
             "\n".join(f"- [{p.kind}] {p.message}" for p in report.problems) or None)
        if report.problems and (attempt < prof.max_attempts or any(not p.minor for p in report.problems)):
            problems = [f"integration check: {p.message}" for p in report.problems]
    if not problems:
        outcome, summary = "completed", result.summary
        break
    print("  rejected: " + "; ".join(problems), flush=True)
    feedback = problems

elapsed = time.monotonic() - t0
res = {"run_id": run_id, "system": "single-agent" + ("+check" if args.check else ""), "model": args.model, "profile": prof.name, "outcome": outcome,
       "attempts": attempt, "problems": problems, "summary": summary, "seconds": round(elapsed, 1),
       "tokens_in": usage["in"], "tokens_out": usage["out"], "model_calls": usage["calls"], "files": written}
log.append("run.finished", {"status": outcome, "summary": summary or "; ".join(problems)})
(ws.mission_dir / "solo_result.json").write_bytes(json.dumps(res, indent=1, ensure_ascii=False).encode("utf-8"))
print(f"\n{outcome} in {elapsed / 60:.1f} min, {usage['calls']} model calls, {usage['in']} tokens in, {usage['out']} out")
sys.exit(0 if outcome == "completed" else 1)
