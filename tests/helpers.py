"""Test doubles: a scripted stand-in for the model, so the engine can be tested end to end with no Ollama."""
import json
import re
import tempfile
from pathlib import Path

from mission_control.engine import EngineConfig, EngineSession
from mission_control.events import check_stream
from mission_control.llm import ChatResult
from mission_control.workspace import init_workspace


def call(name, **args):
    """One tool call in Ollama's wire format."""
    return {"function": {"name": name, "arguments": args}}


def T(id, title, role, deps=(), owns=(), acc=("it works",), goal="Do it."):
    return {"id": id, "title": title, "role": role, "goal": goal, "depends_on": list(deps), "owns": list(owns), "acceptance": list(acc)}


T1 = T("t1", "API contract", "architect", owns=["contracts/**"])
T2 = T("t2", "Backend", "backend", ["t1"], ["backend/**"])
T3 = T("t3", "Frontend", "frontend", ["t1"], ["frontend/**"])
PLAN3 = {"tasks": [T1, T2, T3]}

CONTRACT = call("write_file", path="contracts/api.md", content="# API\nGET /api/menu\n")
APP_PY = call("write_file", path="backend/app.py", content="def menu():\n    return []\n")
INDEX = call("write_file", path="frontend/index.html", content="<!doctype html><title>x</title><h1>Menu</h1>\n")
FINISH = call("finish", summary="done")


def happy_workers():
    return {"t1": [CONTRACT, FINISH], "t2": [APP_PY, FINISH], "t3": [INDEX, FINISH]}


class ScriptedLLM:
    """chat() looks like the real client; replies come from per-task scripts.
    `plan` / `reviews` may be a dict/str or a list of them (consumed one per call, the last one repeats)."""

    def __init__(self, plan=PLAN3, workers=None, reviews=None, on_worker=None, replies=None, diagnoses=None):
        self.plan = plan
        self.workers = {k: list(v) for k, v in (workers or happy_workers()).items()}
        self.reviews = reviews
        self.replies = replies
        self.diagnoses = diagnoses
        self.on_worker = on_worker
        self.calls = []           # (kind, task_id, messages)

    @staticmethod
    def _pick(source, default):
        if source is None:
            return default
        if isinstance(source, list):
            return source.pop(0) if len(source) > 1 else source[0]
        return source

    def chat(self, messages, *, schema=None, tools=None, temperature=None):
        if tools:
            tid = re.search(r"TASK_ID: (\S+)", messages[0]["content"]).group(1)
            self.calls.append(("worker", tid, [dict(m) for m in messages]))
            if self.on_worker:
                self.on_worker(tid, messages)
            step = self.workers[tid].pop(0)
            if isinstance(step, ChatResult):
                return step
            content, calls = step if isinstance(step, tuple) else ("", [step])
            return ChatResult(content=content, tool_calls=calls, tokens_in=100, tokens_out=40)
        props = (schema or {}).get("properties", {})
        if "diagnosis" in props:
            self.calls.append(("diagnose", None, [dict(m) for m in messages]))
            r = self._pick(self.diagnoses, {"diagnosis": "unclear", "user_summary": "The task kept failing.",
                                            "suggestions": ["Retry"], "action": "ask_user", "guidance": "", "tasks": []})
            return ChatResult(content=r if isinstance(r, str) else json.dumps(r), tokens_in=150, tokens_out=50)
        if "reply" in props:
            self.calls.append(("reply", None, [dict(m) for m in messages]))
            m = re.search(r'while you work:\n"(.*)"\n\n', messages[-1]["content"], re.S)
            text = m.group(1) if m else ""
            # default: a message ending in "?" is a question, anything else an instruction
            r = self._pick(self.replies, {"reply": f"Got it: {text}", "is_instruction": not text.rstrip().endswith("?"),
                                          "instruction": text})
            return ChatResult(content=r if isinstance(r, str) else json.dumps(r), tokens_in=50, tokens_out=20)
        if "action" in props:
            self.calls.append(("review", None, [dict(m) for m in messages]))
            r = self._pick(self.reviews, {"action": "keep", "reason": "fine", "tasks": []})
            return ChatResult(content=r if isinstance(r, str) else json.dumps(r), tokens_in=200, tokens_out=60)
        self.calls.append(("plan", None, [dict(m) for m in messages]))
        p = self._pick(self.plan, PLAN3)
        return ChatResult(content=p if isinstance(p, str) else json.dumps(p), tokens_in=300, tokens_out=120)

    def kinds(self, kind, tid=None):
        return [c for c in self.calls if c[0] == kind and (tid is None or c[1] == tid)]


def make_engine(llm, *, review=False, max_steps=14, max_attempts=2, prompt="Build a cafe site", lang="en", factory=None, **extra):
    tmp = tempfile.mkdtemp(prefix="mc-engine-")
    ws = init_workspace(Path(tmp) / "proj")
    cfg = EngineConfig(review=review, max_steps=max_steps, max_attempts=max_attempts, chunk_delay_cap=0, **extra)
    return EngineSession(ws, prompt, llm, model="test-model", lang=lang, config=cfg, llm_factory=factory, sleep=lambda s: None)


def run_engine(llm, *, timeout=20, **kw):
    s = make_engine(llm, **kw)
    s.start()
    s.join(timeout)
    assert not s._thread.is_alive(), "engine did not finish in time"
    return s, s.log.read()


def assert_valid(testcase, events):
    testcase.assertEqual(check_stream(events), [])


class OllamaStandIn(ScriptedLLM):
    """ScriptedLLM that also looks like the Ollama client to the server: it has a host and can list models."""

    host = "http://standin:11434"

    def __init__(self, *a, installed=("test-model:latest", "other:1b"), down=False, sizes=None, **kw):
        super().__init__(*a, **kw)
        self.installed, self.down, self.sizes = list(installed), down, dict(sizes or {})
        self.num_ctx = None

    def list_models_detailed(self):
        return [{"name": n, "parameter_size": self.sizes.get(n, ""), "family": ""} for n in self.list_models()]

    def list_models(self):
        if self.down:
            from mission_control.llm import LLMError
            raise LLMError("cannot reach Ollama at http://standin:11434 (connection refused)")
        return list(self.installed)
