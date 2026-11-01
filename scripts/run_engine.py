#!/usr/bin/env python3
r"""Run the REAL engine headless in the terminal (no browser) against your local Ollama model.

    python scripts/run_engine.py --project C:\Projects\cafe --prompt "Build a cafe website with a menu page"
    python scripts/run_engine.py --project ./out --prompt "..." --model ornith-1.5:9b --no-review

Prints the same events the UI shows. Ctrl+C stops the run cleanly. Exit code 0 = completed, 1 = failed/stopped.
Handy for tuning prompts and for finding out how the model behaves, with the browser out of the picture."""
import argparse
import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_control import utf8_console  # noqa: E402
from mission_control.engine import EngineConfig, EngineSession  # noqa: E402
from mission_control.events import T  # noqa: E402
from mission_control.llm import DEFAULT_MODEL, LLMError, OllamaClient  # noqa: E402
from mission_control.paths import clean_path_input  # noqa: E402
from mission_control.textview import format_event  # noqa: E402
from mission_control.workspace import init_workspace  # noqa: E402

utf8_console()

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--project", required=True, help="project folder (created if missing)")
ap.add_argument("--prompt", required=True)
ap.add_argument("--model", default=DEFAULT_MODEL)
ap.add_argument("--host", default=None, help="Ollama URL, default http://localhost:11434")
ap.add_argument("--num-ctx", type=int, default=None, help="context window; default: no limit set, Ollama decides")
ap.add_argument("--strength", choices=("auto", "small", "medium", "large"), default="auto", help="model strength profile")
ap.add_argument("--on-failure", choices=("ask", "skip", "stop"), default="ask",
                help="when the lead cannot rescue a task: ask here in the terminal, skip it, or stop")
ap.add_argument("--approve-plan", action="store_true", help="show the plan and wait for Enter before any work starts")
ap.add_argument("--no-review", action="store_true", help="skip the plan review after each task (faster)")
ap.add_argument("--lang", choices=("en", "fa"), default="en", help="language of the engine's own status lines")
ap.add_argument("--chunks", action="store_true", help="also print file.chunk events")
ap.add_argument("--input", action="append", default=[], help="input file to copy into inputs/ (repeatable)")
args = ap.parse_args()

llm = OllamaClient(args.model, args.host, num_ctx=args.num_ctx)
factory = lambda m: OllamaClient(m, args.host, num_ctx=args.num_ctx)  # noqa: E731
try:
    installed = llm.list_models()
except LLMError as e:
    sys.exit(f"Cannot reach Ollama: {e}")
if args.model not in installed and f"{args.model}:latest" not in installed:
    sys.exit(f"Model {args.model!r} is not installed. Run: ollama pull {args.model}\nInstalled: {', '.join(installed) or '(none)'}")

ws = init_workspace(clean_path_input(args.project), args.input)
cfg = EngineConfig(review=not args.no_review, strength=args.strength, on_failure=args.on_failure,
                   require_approval=args.approve_plan,
                   context=f"num_ctx={args.num_ctx}" if args.num_ctx else "Ollama default (no limit set by Mission Control)")
session = EngineSession(ws, args.prompt, llm, model=args.model, lang=args.lang, config=cfg, llm_factory=factory)
inbox: "queue.Queue" = queue.Queue()


def show(ev):
    line = format_event(ev, show_chunks=args.chunks)
    if line:
        print(line, flush=True)
    inbox.put(ev)


session.log.add_listener(show)
print(f"project: {ws.root}\nmodel:   {args.model}\n")
session.start()
try:
    while True:                     # questions for the user are asked here, in the main thread
        try:
            ev = inbox.get(timeout=0.3)
        except queue.Empty:
            continue
        if ev.type == T.RUN_FINISHED:
            break
        if ev.type == T.APPROVAL_REQUESTED:
            answer = input("Press Enter to start, or type agent=model pairs (e.g. a_t2=qwen3:32b): ").strip()
            models = dict(p.split("=", 1) for p in answer.split() if "=" in p)
            session.post_approval(models)
        elif ev.type == T.ATTENTION_REQUIRED:
            answer = input("[r]etry (add a note after r), [s]kip or [q]uit: ").strip()
            choice = {"r": "retry", "s": "skip", "q": "stop"}.get(answer[:1].lower(), "skip")
            session.post_resolution(ev.task_id, choice, answer[1:].strip() if choice == "retry" else "")
except KeyboardInterrupt:
    print("\nstopping…")
    session.stop()
final = [e for e in session.log.read() if e.type == T.RUN_FINISHED][-1]
print(f"\n=> {final.data['status']}  {final.data.get('summary', '')}\nlog: {ws.events_path}")
sys.exit(0 if final.data["status"] == "completed" else 1)
