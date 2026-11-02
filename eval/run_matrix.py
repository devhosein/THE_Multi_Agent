#!/usr/bin/env python3
r"""Run the benchmark matrix: every system on every task, N times, then score each result. Resumable.

    python eval/run_matrix.py --runs 3 --root "C:\...\live-runs\bench" --browser msedge

Systems (same model, same machine, same worker loop and tools):
  solo        one agent, no lead, no project check        (the plain baseline)
  solo-check  one agent with Mission Control's project check (check_project + checks after each attempt)
  mc          Mission Control: lead, plan, contract, team, reviews, checks — driven through the app's HTTP API, so
              the run is visible live in the browser; the task's instruction line is sent in the chat during approval

Each finished run appends one line to eval/results/bench/runs.jsonl (scores, time, tokens, outcome); screenshots go to
eval/results/bench/<task>-<system>-<n>/. The Mission Control server must be running (python scripts/serve.py)."""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
TASKS = {"cafe": ("cafe.prompt.txt", "score_site.py"), "taskboard": ("taskboard.prompt.txt", "score_taskboard.py")}
SYSTEMS = ("solo", "solo-check", "mc")


def post(server: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(server + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{path}: HTTP {e.code} {e.read()[:300]!r}") from e


def events(project: Path) -> list[dict]:
    p = project / ".mission" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_for(project: Path, types: set[str], timeout: float) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        hit = next((e for e in events(project) if e["type"] in types), None)
        if hit:
            return hit
        time.sleep(3)
    return None


def ts(e: dict) -> float:
    from datetime import datetime
    return datetime.fromisoformat(e["ts"]).timestamp()


def run_mc(server: str, project: Path, prompt: str, instruction: str, model: str) -> dict:
    post(server, "/api/reset", {})
    post(server, "/api/run", {"prompt": prompt, "project_path": str(project), "mode": "live", "model": model,
                              "require_approval": True, "on_failure": "skip", "review": True})
    if not wait_for(project, {"approval.requested", "run.finished"}, 900):
        raise RuntimeError("no plan within 15 minutes")
    post(server, "/api/message", {"target": "orchestrator", "text": instruction})
    recorded = wait_for(project, {"decision.recorded"}, 180) is not None
    post(server, "/api/approve", {"models": {}})
    if not wait_for(project, {"run.finished"}, 50 * 60):
        post(server, "/api/command", {"command": "stop", "target": "all"})
        wait_for(project, {"run.finished"}, 120)
    ev = events(project)
    first = lambda t: next(e for e in ev if e["type"] == t)                                       # noqa: E731
    fin, asked, granted = first("run.finished"), first("approval.requested"), first("approval.granted")
    usage = [e for e in ev if e["type"] == "usage.updated"]
    plans = [e for e in ev if e["type"] in ("plan.created", "plan.updated")]
    return {"outcome": fin["data"]["status"], "instruction_recorded": recorded,
            "seconds": round(ts(fin) - ts(ev[0]) - (ts(granted) - ts(asked)), 1),
            "tokens_in": usage[-1]["data"]["tokens_in"] if usage else 0,
            "tokens_out": usage[-1]["data"]["tokens_out"] if usage else 0,
            "model_calls": sum(1 for e in ev if e["type"] == "log" and e["data"].get("message", "").startswith("model call")),
            "tasks": len(plans[-1]["data"]["tasks"]) if plans else 0}


def run_solo(project: Path, prompt_file: Path, model: str, check: bool) -> dict:
    cmd = [sys.executable, str(ROOT / "scripts" / "solo_agent.py"), "--project", str(project),
           "--prompt-file", str(prompt_file), "--model", model] + (["--check"] if check else [])
    subprocess.run(cmd, cwd=ROOT, timeout=50 * 60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = json.loads((project / ".mission" / "solo_result.json").read_text(encoding="utf-8"))
    return {k: r[k] for k in ("outcome", "seconds", "tokens_in", "tokens_out", "model_calls")} | {"tasks": 1}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    ap.add_argument("--model", default="ornith-1.5:35b")
    ap.add_argument("--root", required=True, help="folder for the generated projects")
    ap.add_argument("--server", default="http://127.0.0.1:8770")
    ap.add_argument("--browser", default="msedge", help="browser for the scorer")
    a = ap.parse_args()
    out = ROOT / "eval" / "results" / "bench"
    out.mkdir(parents=True, exist_ok=True)
    log = out / "runs.jsonl"
    done = {(r["task"], r["system"], r["run"]) for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())} \
        if log.exists() else set()
    for n in range(1, a.runs + 1):
        for task in a.tasks.split(","):
            prompt_file, scorer = TASKS[task]
            text = (ROOT / "eval" / "tasks" / prompt_file).read_text(encoding="utf-8").strip()
            prompt, instruction = [part.strip() for part in text.split("\n\n", 1)]
            for system in a.systems.split(","):
                if (task, system, n) in done:
                    continue
                label = f"{task}-{system}-{n}"
                project = Path(a.root) / label
                print(f"[{time.strftime('%H:%M')}] {label} …", flush=True)
                try:
                    info = run_mc(a.server, project, prompt, instruction, a.model) if system == "mc" else \
                        run_solo(project, ROOT / "eval" / "tasks" / prompt_file, a.model, system == "solo-check")
                except Exception as e:  # noqa: BLE001 - one broken run must not stop the matrix
                    info = {"outcome": "error", "error": str(e)[:300]}
                subprocess.run([sys.executable, str(ROOT / "eval" / scorer), str(project), "--label", f"bench/{label}",
                                "--browser", a.browser], cwd=ROOT, timeout=600, capture_output=True)
                score_file = out / label / "score.json"
                score = json.loads(score_file.read_text(encoding="utf-8")) if score_file.exists() else {}
                row = {"task": task, "system": system, "run": n, **info, "points": score.get("points", {}),
                       "automatic": score.get("automatic_total"), "notes": score.get("notes", {}), "size": score.get("size")}
                with log.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"           {info.get('outcome')}, {row['automatic']}/85, {info.get('seconds', 0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
