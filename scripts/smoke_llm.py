#!/usr/bin/env python3
"""Smoke-test the local model for exactly what the engine needs from it.

  1. server reachable + model installed
  2. plain chat (Persian)
  3. schema-constrained structured output
  4. tool calling
  5. PLANNING: can it produce a *valid* plan (acyclic, no parallel file conflicts, acceptance criteria)?
     We report first-try success and success-after-repair separately - that gap tells us how much
     the validate-and-retry loop is carrying.

usage: python scripts/smoke_llm.py [--model ornith-1.5:9b] [--host http://localhost:11434]
                                   [--num-ctx 16384] [--think|--no-think] [--runs 3]
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mission_control.llm import (  # noqa: E402
    DEFAULT_MODEL, LLMError, OllamaClient, StructuredOutputError, extract_json,
)
from mission_control.plan import plan_stages  # noqa: E402
from mission_control.planner import make_plan  # noqa: E402

PLAN_PROMPTS = [
    "یه سایت معرفی کافه بساز: منوی محصولات، فرم رزرو میز و یه بک‌اند ساده.",
    "Build a REST API for a todo app with a small web UI and automated tests.",
    "یه ربات تلگرام بساز که قیمت ارز رو بگیره و هر روز صبح گزارش بفرسته.",
    "Create a command-line tool that converts a folder of Markdown files into a static website.",
    "یه اپ وب برای مدیریت هزینه‌های شخصی با نمودار ماهانه بساز.",
]

ap = argparse.ArgumentParser()
ap.add_argument("--model", default=DEFAULT_MODEL)
ap.add_argument("--host", default=None)
ap.add_argument("--num-ctx", type=int, default=16384)
g = ap.add_mutually_exclusive_group()
g.add_argument("--think", dest="think", action="store_true", default=None)
g.add_argument("--no-think", dest="think", action="store_false")
ap.add_argument("--runs", type=int, default=3, help="how many planner prompts to try (max %d)" % len(PLAN_PROMPTS))
args = ap.parse_args()

llm = OllamaClient(args.model, args.host, num_ctx=args.num_ctx, think=args.think)
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return ok


def timed(fn):
    t = time.time()
    result = fn()
    return result, time.time() - t


# 1 ------------------------------------------------------------------ reachability
try:
    models = llm.list_models()
    wanted = {args.model, args.model + ":latest"}
    ok = any(m in wanted for m in models)
    record("server + model installed", ok, f"({llm.host}) " + ("" if ok else f"model {args.model!r} not in {models}; run: ollama pull {args.model}"))
    if not ok:
        sys.exit(1)
except LLMError as e:
    record("server + model installed", False, str(e))
    sys.exit(1)

try:
    # 2 ------------------------------------------------------------------ plain chat
    res, secs = timed(lambda: llm.chat([{"role": "user", "content": "در یک جمله درباره‌ی قهوه بنویس."}], temperature=0.3))
    record("plain chat (Persian)", bool(res.content.strip()),
           f"{res.tokens_out} tok, {res.tokens_per_second:.1f} tok/s, {secs:.1f}s | {res.content.strip()[:70]!r}")

    # 3 ------------------------------------------------------------------ structured output
    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}
    res, _ = timed(lambda: llm.chat([{"role": "user", "content": 'What is 17 + 25? Reply as JSON {"answer": <int>}.'}],
                                    schema=schema, temperature=0))
    try:
        val = extract_json(res.content)
        record("structured output (schema)", val.get("answer") == 42, f"-> {val}")
    except ValueError as e:
        record("structured output (schema)", False, f"unparseable: {res.content[:80]!r} ({e})")

    # 4 ------------------------------------------------------------------ tool calling
    tools = [{"type": "function", "function": {
        "name": "write_file", "description": "Write a text file inside the project.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}}]
    res, _ = timed(lambda: llm.chat(
        [{"role": "user", "content": "Create a file named hello.txt that contains the word hi. Use the tool."}],
        tools=tools, temperature=0))
    call = res.tool_calls[0]["function"] if res.tool_calls else None
    args_ok = bool(call) and call["name"] == "write_file" and {"path", "content"} <= set(call.get("arguments", {}))
    record("tool calling", args_ok, f"-> {call}" if call else f"no tool call; model said: {res.content[:80]!r}")

    # 5 ------------------------------------------------------------------ planning
    first_try = repaired = failed = 0
    shown = False
    n = min(args.runs, len(PLAN_PROMPTS))
    for prompt in PLAN_PROMPTS[:n]:
        t = time.time()
        try:
            plan, sr = make_plan(llm, prompt)
        except StructuredOutputError as e:
            failed += 1
            print(f"  plan FAILED after {e.attempts} attempts: {prompt[:50]!r}\n    " + "\n    ".join(e.errors[:4]), flush=True)
            continue
        except LLMError as e:
            failed += 1
            print(f"  plan ERROR: {e}", flush=True)
            continue
        if sr.attempts == 1:
            first_try += 1
        else:
            repaired += 1
        print(f"  plan ok in {sr.attempts} attempt(s), {len(plan.tasks)} tasks, {time.time() - t:.0f}s: {prompt[:50]!r}", flush=True)
        if not shown:
            shown = True
            for i, wave in enumerate(plan_stages(plan), 1):
                for tid in wave:
                    tk = plan.by_id()[tid]
                    print(f"      wave {i}  {tid:<10s} [{tk.role}] {tk.title}  owns={list(tk.owns)} deps={list(tk.depends_on)}")
    record("planning produces a VALID plan", failed == 0,
           f"first-try {first_try}/{n}, after repair {repaired}/{n}, failed {failed}/{n}")
except LLMError as e:
    record("llm call", False, str(e))

print("\n" + "=" * 60)
essential = results[:4]
print(f"essentials: {sum(ok for _, ok, _ in essential)}/{len(essential)} passed")
if len(results) > 4:
    print("planning: " + results[4][2])
sys.exit(0 if all(ok for _, ok, _ in essential) else 1)
