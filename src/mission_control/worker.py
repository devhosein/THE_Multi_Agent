"""The worker agent loop: ask the model, run the tools it calls, repeat until it calls `finish`.

Every defence below comes from a failure seen in a real run (see docs/FAILURES.md):

  * transient model errors ("CUDA error: illegal memory access")  -> wait and call again, the attempt is not lost
  * a tool call cut off mid-JSON ("unexpected end of JSON input")  -> shrink the history, ask for smaller pieces
    (write_file + append_file), call again
  * the same call over and over (a model rewrote test.sh 14 times) -> warn on the 3rd repeat, stop on the 5th with a
    clear diagnosis; there is no small fixed step budget any more, only a high safety cap
  * an answer that never ends (thinking, no action; 600 s timeouts) -> the client caps the output; the cut-off
    answer is dropped and the agent is told to stop deliberating and make its next tool call
  * a history that grows with every file written or read             -> compaction: written content and old reads
    are replaced by short notes (the files are on disk; the model can read them again). A note keeps the names other
    files must match (classes, ids): without them an agent styled .form-group and then wrote form-row in the HTML
  * a user message                                                  -> the agent answers it at its next step,
    then carries on with the message taken into account
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .integration import ID_LOOKUP_RE, css_classes
from .llm import ChatResult, LLMClient, LLMError, classify_llm_error
from .tools import REMOVED_NOTE, TOOL_SPECS, ToolBox

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
WRITE_TOOLS = ("write_file", "append_file")
_HTML_ATTR = re.compile(r"""\b(id|class)\s*=\s*["']([^"'<>{}$]+)["']""")
_JS_CLASS = re.compile(r"""(?:classList\.(?:add|remove|toggle|contains)\(\s*|className\s*=\s*)['"]([\w\s-]+)['"]|"""
                       r"""querySelector(?:All)?\(\s*['"]\.([\w-]+)""")

LogFn = Callable[..., None]   # log(level, source, message, detail=None)


@dataclass
class WorkerResult:
    ok: bool
    summary: str = ""
    error: str | None = None
    kind: str = ""          # "" | loop | step_cap | model_fatal | model_unavailable | truncated | no_work
    steps: int = 0


@dataclass
class WorkerLimits:
    max_steps: int = 80                     # safety net only; loops are caught long before this
    loop_warn: int = 3
    loop_stop: int = 5
    transient_retries: int = 3
    truncation_retries: int = 3
    backoff: tuple[float, ...] = (2.0, 5.0, 10.0)
    temperature: float = 0.2


def clean_text(text: str | None) -> str:
    return _THINK.sub("", text or "").strip()


def parse_arguments(raw: Any) -> Any:
    """Ollama normally gives a dict; some models give a JSON string. Anything else is passed through."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw if raw is not None else {}


_BOILERPLATE = re.compile(r"^Ollama returned (HTTP \d+) for \S+: |an error was encountered while running the model: ?", re.I)


def _short(err: BaseException | str, n: int = 160) -> str:
    """The reason, not the wrapping: 'Ollama returned HTTP 500 for /api/chat: an error was encountered while running the
    model: CUDA error: ...' becomes 'HTTP 500: CUDA error: ...'."""
    s = str(err).replace("\n", " ")
    s = _BOILERPLATE.sub(lambda m: f"{m.group(1)}: " if m.group(1) else "", s).strip()
    return s if len(s) <= n else s[:n] + "…"


def call_model(llm: LLMClient, messages: list[dict[str, Any]], *, tools=None, schema=None, temperature: float | None = None,
               limits: WorkerLimits, log: LogFn, source: str, sleep: Callable[[float], None] = time.sleep) -> ChatResult:
    """One model call that survives transient failures (GPU/runner hiccups, dropped connections)."""
    for i in range(limits.transient_retries + 1):
        try:
            t0 = time.monotonic()
            res = llm.chat(messages, tools=tools, schema=schema, temperature=temperature)
            log("debug", source, f"model call {time.monotonic() - t0:.1f}s, {res.tokens_in} tokens in, {res.tokens_out} out"
                + (f", stopped: {res.done_reason}" if res.done_reason and res.done_reason != "stop" else ""))
            return res
        except LLMError as e:
            if classify_llm_error(e) != "transient" or i >= limits.transient_retries:
                raise
            wait = limits.backoff[min(i, len(limits.backoff) - 1)]
            log("warn", source, f"Model call failed ({_short(e, 120)}); retrying in {wait:g}s ({i + 1}/{limits.transient_retries})", str(e))
            sleep(wait)
    raise AssertionError("unreachable")


def interface(path: str, content: str, limit: int = 60) -> str:
    """The names in a file that OTHER files must match: classes a stylesheet styles, ids and classes a page has, ids
    and classes a script uses. Kept in the history when the content itself is dropped."""
    ext = path.rsplit(".", 1)[-1].lower()
    parts: list[tuple[str, set[str]]] = []
    if ext == "css":
        parts.append(("styles the classes", css_classes(content)))
    elif ext in ("html", "htm"):
        ids: set[str] = set()
        classes: set[str] = set()
        for kind, value in _HTML_ATTR.findall(content):
            if kind == "id":
                ids.add(value.strip())
            else:
                classes.update(value.split())
        parts += [("has the ids", ids), ("uses the classes", classes)]
    elif ext in ("js", "mjs"):
        parts += [("looks up the ids", {m.group(2) or m.group(4) for m in ID_LOOKUP_RE.finditer(content)}),
                  ("uses the classes", {c for m in _JS_CLASS.finditer(content) for c in (m.group(1) or m.group(2)).split()})]
    shown = []
    for label, names in parts:
        if names:
            ordered = sorted(names)
            shown.append(f"{label}: {', '.join(ordered[:limit])}" + (" …" if len(ordered) > limit else ""))
    return "; ".join(shown)


def compact(messages: list[dict[str, Any]], *, aggressive: bool = False) -> int:
    """Shrink the conversation in place; returns the number of characters saved.

    - content already written to disk leaves the history (in every assistant tool call but the newest): the call keeps
      only its path, and the TOOL RESULT says what was removed and which names the file defines. The note is not put
      in the call itself: models imitate their own calls, and live runs v4/v7/v8 wrote such a note as file content
    - only the newest file reads stay complete; older ones are shortened
    - aggressive mode (after a cut-off answer) also shortens old assistant prose"""
    saved = 0
    assistants = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    newest_assistant = assistants[-1] if assistants else -1
    for i in assistants:
        m = messages[i]
        if i != newest_assistant or aggressive:
            results = [j for j in range(i + 1, len(messages)) if messages[j].get("role") == "tool"]
            nxt = next((a for a in assistants if a > i), len(messages))
            results = [j for j in results if j < nxt]               # this call's results, in the order of the calls
            calls = []
            for k, c in enumerate(m.get("tool_calls") or []):
                fn = c.get("function", c) if isinstance(c, dict) else {}
                args = parse_arguments(fn.get("arguments")) if isinstance(fn, dict) else None
                if isinstance(fn, dict) and fn.get("name") in WRITE_TOOLS and isinstance(args, dict) \
                        and isinstance(args.get("content"), str):
                    n = len(args["content"])
                    saved += n
                    names = interface(str(args.get("path", "")), args["content"])
                    c = {**c, "function": {**fn, "arguments": {k2: v for k2, v in args.items() if k2 != "content"}}}
                    if k < len(results):
                        r = messages[results[k]]
                        r["content"] = (r.get("content") or "") + f"\n[{n} {REMOVED_NOTE}" \
                            + (f" It {names}." if names else "") + "]"
                calls.append(c)
            if calls:
                m["tool_calls"] = calls
        if aggressive and i not in assistants[-2:] and len(m.get("content") or "") > 600:
            saved += len(m["content"]) - 600
            m["content"] = m["content"][:600] + " …"
    reads = [i for i, m in enumerate(messages) if m.get("role") == "tool" and m.get("tool_name") == "read_file"]
    keep_full = 1 if aggressive else 2
    limit = 300 if aggressive else 800
    for i in reads[:-keep_full] if keep_full else reads:
        c = messages[i].get("content") or ""
        if len(c) > limit + 80 and "[older read shortened" not in c:
            saved += len(c) - limit
            messages[i]["content"] = c[:limit] + "\n…[older read shortened; call read_file again if you need it]"
    return saved


def _signature(name: str, args: Any) -> str:
    return hashlib.sha1(f"{name}|{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}".encode()).hexdigest()


def _describe(name: str, args: Any) -> str:
    return f"{name} {args.get('path', '')}".strip() if isinstance(args, dict) else name


TRUNCATION_HINT = ("Your last answer was cut off before it finished (the tool call was too long). Keep each tool call "
                   "smaller: write a long file in parts - write_file with the first part (about 150 lines), then append_file "
                   "for each next part. Keep your explanations short. Continue now.")

RUNAWAY_HINT = ("Your last answer ran out of room while you were still thinking, before you made any tool call. Do not "
                "think everything through again: decide the single next step and make that tool call now. Keep your "
                "reasoning short.")


def run_worker(
    llm: LLMClient,
    toolbox: ToolBox,
    system: str,
    user: str,
    *,
    gate: Callable[[], None],
    deliver: Callable[[], list[str]],
    reply: Callable[[list[dict[str, Any]], str], str | None],
    on_message: Callable[[str], None],
    on_usage: Callable[[int, int], None],
    log: LogFn = lambda *a, **k: None,
    limits: WorkerLimits | None = None,
    sleep: Callable[[float], None] = time.sleep,
    deliver_lead: Callable[[], list[str]] = lambda: [],
) -> WorkerResult:
    """`reply(messages, text)` answers a user message in this agent's voice and returns the instruction it carries
    (or None for a plain question). `deliver()` hands over the user's messages at each safe point, `deliver_lead()`
    the lead agent's answers to this agent's reports (and any other word from the lead)."""
    lim = limits or WorkerLimits()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    nudged = False
    truncations = 0
    seen: dict[str, int] = {}
    for step in range(1, lim.max_steps + 1):
        gate()
        for text in deliver():                       # answer first, then carry on with it in mind
            instruction = reply(messages, text)
            # a question is NOT repeated here: shown the question again, models answered it a second time (real run)
            messages.append({"role": "user", "content": (
                f"Instruction from the user (you have already replied to them): {instruction}\nApply it to your work."
                if instruction else "(The user's question has already been answered for you. Do not answer it again; "
                "continue your task.)")})
        for text in deliver_lead():
            messages.append({"role": "user", "content": f"Message from the lead agent (your project manager): {text}\n"
                             "Follow it. Then continue your task."})
        compact(messages)
        try:
            res = call_model(llm, messages, tools=TOOL_SPECS, temperature=lim.temperature, limits=lim, log=log,
                             source="worker", sleep=sleep)
        except LLMError as e:
            kind = classify_llm_error(e)
            if kind == "truncated" and truncations < lim.truncation_retries:
                truncations += 1
                saved = compact(messages, aggressive=True)
                log("warn", "worker", f"The model's answer was cut off mid tool call; history shrunk by {saved} characters, "
                    f"asking for smaller pieces ({truncations}/{lim.truncation_retries})", str(e))
                messages.append({"role": "user", "content": TRUNCATION_HINT})
                continue
            if kind == "truncated":
                return WorkerResult(False, error="the model's answers kept getting cut off before the tool call was complete "
                                    "(probably the context window is too small for this task)", kind="truncated", steps=step)
            if kind == "fatal":
                return WorkerResult(False, error=f"the model cannot be used: {_short(e)}", kind="model_fatal", steps=step)
            return WorkerResult(False, error=f"the model kept failing: {_short(e)}", kind="model_unavailable", steps=step)
        on_usage(res.tokens_in, res.tokens_out)
        text = clean_text(res.content)
        if text:
            on_message(text)
        reply_msg: dict[str, Any] = {"role": "assistant", "content": res.content or ""}
        if res.tool_calls:
            reply_msg["tool_calls"] = res.tool_calls
        messages.append(reply_msg)

        if not res.tool_calls:
            if res.done_reason == "length" and truncations < lim.truncation_retries:
                truncations += 1
                compact(messages, aggressive=True)
                runaway = len(text) < 2000            # (almost) all of it was thinking: it never got to act
                log("warn", "worker", "The model " + ("thought until it ran out of room without acting; asking it to act now"
                    if runaway else "ran out of room before calling a tool; asking for shorter steps"),
                    f"{res.tokens_out} tokens generated")
                messages.append({"role": "user", "content": RUNAWAY_HINT if runaway else TRUNCATION_HINT})
                continue
            if toolbox.finished is not None or toolbox.written:   # it worked and just forgot to say "finish"
                return WorkerResult(True, summary=toolbox.finished or text or "(no summary given)", steps=step)
            if nudged:
                return WorkerResult(False, error="the agent produced no work (no files written, no tool calls)", kind="no_work", steps=step)
            nudged = True
            messages.append({"role": "user", "content": "You have not used any tool yet. Use write_file to create the files for "
                             "your task (append_file for long ones), then call finish."})
            continue

        for call in res.tool_calls:
            gate()
            fn = call.get("function", call) if isinstance(call, dict) else {}
            name = str(fn.get("name")) if isinstance(fn, dict) else "None"
            args = parse_arguments(fn.get("arguments") if isinstance(fn, dict) else None)
            result = toolbox.call(name, args)
            if result.startswith("ERROR"):
                log("warn", "tool", f"{_describe(name, args)} refused: {result[7:][:160]}")
            messages.append({"role": "tool", "tool_name": name, "content": result})
            if toolbox.finished is not None:
                return WorkerResult(True, summary=toolbox.finished, steps=step)
            # re-checking the project after each change is normal; the same RESULT again and again is the loop
            sig = _signature(name, {"result": result} if name == "check_project" else args)
            seen[sig] = seen.get(sig, 0) + 1
            if seen[sig] >= lim.loop_stop:
                return WorkerResult(False, kind="loop", steps=step,
                                    error=f"stuck in a loop: made the same call ({_describe(name, args)}) {seen[sig]} times")
            if seen[sig] == lim.loop_warn:
                log("warn", "worker", f"Repeated call detected: {_describe(name, args)} x{seen[sig]}; told the agent to change approach")
                messages.append({"role": "user", "content": (
                    f"You have now made exactly the same call ({_describe(name, args)}) {seen[sig]} times. Repeating it changes "
                    "nothing. You cannot run programs or tests here, so do not try. Do something different: write the next file "
                    "your task needs, or call finish if your files are complete.")})
    return WorkerResult(False, error=f"the agent did not finish within {lim.max_steps} model calls", kind="step_cap", steps=lim.max_steps)
