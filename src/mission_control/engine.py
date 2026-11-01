"""The real engine: a lead agent that plans, gets your go-ahead, briefs workers, checks results, rescues failures and
revises the plan while the run is in progress.

  prompt ─► plan ─► one agent per task ─► [approval: you may change each agent's model] ─► for each ready task:
     worker loop (tools) ─► checks ─► verified
                       └─► failed ─► more attempts ─► the LEAD diagnoses the root cause and rescues the task
                                     (retry with concrete guidance, or split it into smaller tasks)
                                  └─► still failing ─► YOU decide (retry, retry with a note, skip, stop)
     ─► the lead reviews the remaining plan ─► next task ─► run.finished

Your messages are answered right away (at the next safe point), in the voice of the agent you wrote to; real
instructions also go into the shared decision ledger, questions just get an answer.

Execution is sequential (the plan already encodes what could run in parallel). Pause/stop act at safe points; an
in-flight model call is never killed halfway. Everything that happens is also written as `log` events, which feed the
Logs panel and the shareable diagnostic report."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .checks import check_task
from .eventlog import EventLog
from .events import AGENT_TRANSITIONS, COMMANDS, ORCHESTRATOR_ID, RESOLUTIONS, T
from .llm import LLMClient, LLMError, StructuredOutputError
from .integration import Problem, check_integration
from .orchestrator import (INFRA_KINDS, answer_user, build_worker_system, build_worker_user, diagnose_failure,
                           handle_note, lead_context, review_plan)
from .plan import Plan, PlanError, Task, diff_plans, path_allowed, plan_stages, validate_replan
from .planner import make_plan
from .profiles import Profile, detect, resolve
from .session import MAX_MESSAGE_CHARS, SessionError, rotate_events_file
from .tools import ToolBox
from .worker import WorkerLimits, run_worker
from .workspace import Workspace, append_decision, save_plan

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "analyzing": "Analyzing your request…",
        "planning_failed": "Could not produce a valid plan: {error}",
        "plan_ready": "Plan ready: {n} tasks. “{first}” goes first.",
        "awaiting": "Plan ready. Review the team, change models if you like, then start the work.",
        "approved": "Approved. Starting the work.",
        "starting": "Starting “{title}”",
        "retrying": "“{title}” did not pass the checks; trying again",
        "task_failed": "“{title}” failed: {problems}",
        "diagnosing": "“{title}” failed; working out why…",
        "rescue_retry": "Rescuing “{title}”: retrying with new guidance",
        "rescue_split": "Rescuing “{title}”: splitting it into smaller tasks",
        "needs_user": "“{title}” needs your decision",
        "user_retry": "Retrying “{title}” as you asked",
        "user_skip": "Skipping “{title}” as you asked",
        "skipped": "Skipping “{title}”: it depends on work that did not succeed",
        "reviewing": "Reviewing the plan…",
        "plan_kept": "Plan checked, no changes needed",
        "review_failed": "Plan review did not work out; keeping the current plan",
        "plan_updated": "Plan updated: {reason}",
        "done_ok": "All tasks finished and passed the checks.",
        "done_failed": "The run finished with problems: {bad} of {n} tasks did not complete.",
        "stopped": "Stopped by the user",
        "continuing": "Continuing the previous run: {n} task(s) to redo.",
        "reply_failed": "I got your message and will take it into account.",
        "pausing": "Pausing at the next safe point; the current step finishes first.",
        "not_paused": "The run is not paused.",
        "already_pausing": "Already pausing.",
        "finished_chat": "This run has finished. Start a new run to continue.",
        "target_note": "Pause and stop apply to the whole run (agents currently work one after another).",
        "note_received": "{agent} reported a problem to the lead",
        "integration_ok": "Integration check passed: the parts work together",
        "integration_own": "“{title}”: the integration check found problems in its own files",
        "integration_cross": "The integration check found {n} problem(s) between agents; adding a fix task",
        "final_check": "Final check of the whole project…",
        "done_integration": "All tasks finished, but the parts still do not work together: {n} integration problem(s) remain.",
    },
    "fa": {
        "analyzing": "در حال تحلیل درخواست…",
        "planning_failed": "ساخت پلن معتبر ممکن نشد: {error}",
        "plan_ready": "پلن آماده است: {n} تسک. اول «{first}» انجام می‌شود.",
        "awaiting": "پلن آماده است. تیم را بررسی کن، اگر خواستی مدل‌ها را عوض کن و بعد کار را شروع کن.",
        "approved": "تأیید شد. کار شروع می‌شود.",
        "starting": "شروع «{title}»",
        "retrying": "«{title}» از بررسی‌ها رد نشد؛ دوباره تلاش می‌شود",
        "task_failed": "«{title}» ناموفق بود: {problems}",
        "diagnosing": "«{title}» ناموفق بود؛ در حال پیدا کردن علت…",
        "rescue_retry": "نجات «{title}»: تلاش دوباره با راهنمایی جدید",
        "rescue_split": "نجات «{title}»: شکستن آن به تسک‌های کوچک‌تر",
        "needs_user": "«{title}» به تصمیم تو نیاز دارد",
        "user_retry": "تلاش دوباره برای «{title}» طبق درخواست تو",
        "user_skip": "رد کردن «{title}» طبق درخواست تو",
        "skipped": "«{title}» رد شد: به کاری وابسته است که موفق نبود",
        "reviewing": "در حال مرور پلن…",
        "plan_kept": "پلن بررسی شد، تغییری لازم نیست",
        "review_failed": "مرور پلن به نتیجه نرسید؛ پلن فعلی حفظ می‌شود",
        "plan_updated": "پلن به‌روز شد: {reason}",
        "done_ok": "همه‌ی تسک‌ها انجام شد و بررسی‌ها را گذراندند.",
        "done_failed": "اجرا با مشکل تمام شد: {bad} تسک از {n} کامل نشد.",
        "stopped": "متوقف‌شده توسط کاربر",
        "continuing": "ادامه‌ی اجرای قبلی: {n} تسک دوباره انجام می‌شود.",
        "reply_failed": "پیامت را گرفتم و آن را در نظر می‌گیرم.",
        "pausing": "در نزدیک‌ترین نقطه‌ی امن متوقف می‌شود؛ گام جاری اول تمام می‌شود.",
        "not_paused": "اجرا متوقف نیست.",
        "already_pausing": "همین حالا در حال توقف است.",
        "finished_chat": "این اجرا تمام شده است. برای ادامه یک اجرای جدید شروع کن.",
        "target_note": "توقف و پایان برای کل اجرا اعمال می‌شود (ایجنت‌ها فعلاً یکی‌یکی کار می‌کنند).",
        "note_received": "{agent} مشکلی را به ایجنت راس گزارش داد",
        "integration_ok": "بررسی اتصال موفق بود: بخش‌ها با هم کار می‌کنند",
        "integration_own": "«{title}»: بررسی اتصال در فایل‌های خودش مشکل پیدا کرد",
        "integration_cross": "بررسی اتصال {n} مشکل بین ایجنت‌ها پیدا کرد؛ یک تسک اصلاح اضافه می‌شود",
        "final_check": "بررسی نهایی کل پروژه…",
        "done_integration": "همه‌ی تسک‌ها تمام شد، ولی بخش‌ها هنوز با هم کار نمی‌کنند: {n} مشکل اتصال باقی مانده.",
    },
}


class StopRun(Exception):
    """Raised inside the engine thread to unwind cleanly when the user stops the run."""


@dataclass
class EngineConfig:
    strength: str = "auto"              # auto | small | medium | large  (the model-strength dial)
    max_steps: int | None = None        # None: the profile's safety net (loops are detected separately)
    max_attempts: int | None = None     # None: from the profile
    recoveries: int | None = None       # None: from the profile (how often the lead may rescue a task on its own)
    review: bool = True                 # the lead reviews the remaining plan after every task
    require_approval: bool = False      # wait for the user's go-ahead after planning (the UI turns this on)
    on_failure: str = "skip"            # what happens when the lead cannot rescue a task: ask | skip | stop
    plan_retries: int = 2
    review_retries: int = 2
    chunk_delay_cap: float = 0.02       # pacing of the file "typing" events (0 = instant)
    backoff: tuple[float, ...] = (2.0, 5.0, 10.0)
    context: str = "Ollama default"     # informational: shown in the logs and the report
    integration: bool = True            # run the project after each task and at the end (integration.py)
    integration_fixes: int = 3          # how many fix tasks the engine may add for problems between agents


@dataclass
class Carry:
    """What a continuation run inherits from the run it continues ("Retry failed tasks")."""
    plan: Plan
    statuses: dict[str, str]
    agent_models: dict[str, str]
    results: dict[str, str] = field(default_factory=dict)
    written: dict[str, list[str]] = field(default_factory=dict)
    decision_n: int = 0


class EngineSession:
    mode = "live"

    def __init__(self, ws: Workspace, prompt: str, llm: LLMClient, *, model: str, lang: str = "en",
                 run_id: str | None = None, config: EngineConfig | None = None,
                 llm_factory: Callable[[str], LLMClient] | None = None, carry: Carry | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.ws, self.prompt, self.llm, self.model = ws, prompt, llm, model
        self.lang = lang if lang in STRINGS else "en"
        self.cfg = config or EngineConfig()
        self.llm_factory = llm_factory
        self.carry = carry
        self._sleep = sleep
        self.run_id = run_id or "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        rotate_events_file(ws)
        self.log = EventLog(ws.events_path, self.run_id)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pause_requested = False
        self._ended = False
        self._agents: set[str] = {ORCHESTRATOR_ID}
        self._agent_state: dict[str, str] = {}
        self._agent_for: dict[str, str] = {}
        self._agent_model: dict[str, str] = {}
        self._clients: dict[str, LLMClient] = {model: llm}
        self._model_sizes: dict[str, str] = {}
        self._installed: set[str] | None = None
        self._plan: Plan | None = None
        self._status: dict[str, str] = {}
        self._results: dict[str, str] = {}
        self._written: dict[str, list[str]] = {}
        self._notes: list[tuple[str, str]] = []
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._last_words: dict[str, list[str]] = {}
        self._inbox: list[tuple[str, str]] = []
        self._pending_for_agent: dict[str, list[str]] = {}
        self._lead_instructions: list[str] = []
        self._escalations: list[tuple[str | None, str | None, str, str]] = []   # (task, agent, who, report)
        self._lead_msgs: dict[str, list[str]] = {}       # the lead's answers, delivered at the agent's next step
        self._reports: dict[str, list[str]] = {}         # every report per task (for the failure diagnosis)
        self._serving_reports = False
        self._cross: list[Problem] = []                  # integration problems between agents, found after a task
        self._fixes = 0
        self._final_reviews = 0
        self._integration_left: list[Problem] = []
        self._running_agent: str | None = None
        self._awaiting_approval = False
        self._approved: dict[str, str] | None = None
        self._attention: str | None = None
        self._decision: tuple[str, str] | None = None
        self._decision_n = carry.decision_n if carry else 0
        self._tin = self._tout = 0
        self._thread = threading.Thread(target=self._main, name="engine", daemon=True)

    @classmethod
    def continuation(cls, old: "EngineSession", **kw: Any) -> "EngineSession":
        """A new run that keeps every verified task of `old` and redoes the rest."""
        assert old._plan is not None
        carry = Carry(old._plan, dict(old._status), dict(old._agent_model), dict(old._results),
                      {k: list(v) for k, v in old._written.items()}, old._decision_n)
        cfg = kw.pop("config", None) or EngineConfig(**{**old.cfg.__dict__, "require_approval": False})
        return cls(old.ws, old.prompt, old.llm, model=old.model, lang=old.lang, config=cfg,
                   llm_factory=old.llm_factory, carry=carry, sleep=old._sleep, **kw)

    # ------------------------------------------------------------ public interface
    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        return not self._ended

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._end("stopped", self._s("stopped"))

    @property
    def can_continue(self) -> bool:
        return self._ended and self._plan is not None and any(s != "verified" for s in self._status.values())

    def post_message(self, target: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            raise SessionError("The message is empty", code="message_empty")
        if len(text) > MAX_MESSAGE_CHARS:
            raise SessionError(f"The message is longer than {MAX_MESSAGE_CHARS} characters", code="message_too_long",
                               params={"limit": MAX_MESSAGE_CHARS})
        with self._lock:
            if self._ended:
                raise SessionError(self._s("finished_chat"), 409, "run_finished")
            if target not in self._agents:
                raise SessionError(f"No agent with id “{target}”", 404, "no_agent", {"target": target})
            self.log.append(T.AGENT_MESSAGE, {"role": "user", "text": text}, agent_id=target)
            self._inbox.append((target, text))
        self._wake.set()

    def post_command(self, command: str, target: str = "all") -> dict[str, Any]:
        if command not in COMMANDS:
            raise SessionError(f"Invalid command: {command}", code="bad_command", params={"command": command})
        with self._lock:
            if target != "all" and target not in self._agents:
                raise SessionError(f"No agent with id “{target}”", 404, "no_agent", {"target": target})
            if self._ended:
                raise SessionError(self._s("finished_chat"), 409, "run_finished")
            note = self._s("target_note") if target not in ("all", ORCHESTRATOR_ID) else ""
            if command == "stop":
                self.log.append(T.USER_COMMAND, {"command": "stop", "target": target})
                self.stop()
                return {"applied": True, "note": ""}
            if command == "pause":
                if self._pause_requested:
                    return {"applied": False, "note": self._s("already_pausing")}
                self.log.append(T.USER_COMMAND, {"command": "pause", "target": target})
                self._pause_requested = True
                return {"applied": True, "note": (self._s("pausing") + " " + note).strip()}
            if not self._pause_requested:
                return {"applied": False, "note": self._s("not_paused")}
            self.log.append(T.USER_COMMAND, {"command": "resume", "target": target})
            self._pause_requested = False
        self._wake.set()
        return {"applied": True, "note": ""}

    def post_approval(self, models: dict[str, str] | None = None) -> dict[str, Any]:
        """The user's go-ahead after planning, optionally with another model for some agents."""
        models = {k: str(v).strip() for k, v in (models or {}).items()}
        with self._lock:
            if self._ended:
                raise SessionError(self._s("finished_chat"), 409, "run_finished")
            if not self._awaiting_approval:
                raise SessionError("There is no plan waiting for approval", 409, "not_awaiting")
            for aid, m in models.items():
                if aid not in self._agent_model:
                    raise SessionError(f"No agent with id “{aid}”", 404, "no_agent", {"target": aid})
                if not m:
                    raise SessionError("Model name is empty", 400, "model_missing", {"model": m, "installed": []})
                if self._installed is not None and m not in self._installed and f"{m}:latest" not in self._installed:
                    raise SessionError(f"The model “{m}” is not installed in Ollama. Run: ollama pull {m}", 400,
                                       "model_missing", {"model": m, "installed": sorted(self._installed)[:20]})
            self._approved = {**self._agent_model, **models}
        self._wake.set()
        return {"applied": True}

    def post_resolution(self, task_id: str, decision: str, note: str = "") -> dict[str, Any]:
        """The user's decision on a task the lead could not rescue."""
        if decision not in RESOLUTIONS:
            raise SessionError(f"Invalid decision: {decision}", 400, "bad_decision")
        with self._lock:
            if self._ended:
                raise SessionError(self._s("finished_chat"), 409, "run_finished")
            if self._attention != task_id:
                raise SessionError("That task is not waiting for a decision", 409, "no_attention")
            self._decision = (decision, (note or "").strip()[:MAX_MESSAGE_CHARS])
        self._wake.set()
        return {"applied": True}

    # ------------------------------------------------------------------- plumbing
    def _s(self, key: str, **kw: Any) -> str:
        return STRINGS[self.lang][key].format(**kw)

    def _emit(self, type_: str, data: dict[str, Any] | None = None, *, agent_id: str | None = None, task_id: str | None = None):
        with self._lock:
            if self._ended:
                return None  # the run is over (e.g. stopped): late events from the engine thread are dropped
            return self.log.append(type_, data, agent_id=agent_id, task_id=task_id)

    def _end(self, status: str, summary: str = "") -> None:
        with self._lock:
            if self._ended:
                return
            self._ended = True
            data: dict[str, Any] = {"status": status}
            if summary:
                data["summary"] = summary
            self.log.append(T.RUN_FINISHED, data)

    def _log(self, level: str, source: str, message: str, detail: str | None = None, *, task_id: str | None = None,
             agent_id: str | None = None) -> None:
        data: dict[str, Any] = {"level": level, "source": source, "message": message}
        if detail:
            data["detail"] = detail[:4000]
        if agent_id and agent_id not in self._agents:
            agent_id = None
        self._emit(T.LOG, data, agent_id=agent_id, task_id=task_id)

    def _line(self, key: str, **kw: Any) -> None:
        self._emit(T.ORCH_STATUS, {"text": self._s(key, **kw)})

    def _say(self, text: str, agent_id: str = ORCHESTRATOR_ID) -> None:
        self._emit(T.AGENT_MESSAGE, {"role": "agent", "text": text}, agent_id=agent_id)

    def _set_task(self, tid: str, status: str, **extra: Any) -> None:
        self._status[tid] = status
        self._emit(T.TASK_STATUS, {"status": status, **extra}, task_id=tid)

    def _set_agent(self, aid: str, status: str) -> None:
        cur = self._agent_state.get(aid)
        if cur == status or status not in AGENT_TRANSITIONS.get(cur or "", set()):
            return
        self._agent_state[aid] = status
        self._emit(T.AGENT_STATUS, {"status": status}, agent_id=aid)

    def _usage(self, tin: int, tout: int) -> None:
        with self._lock:
            self._tin += tin
            self._tout += tout
            self._emit(T.USAGE, {"tokens_in": self._tin, "tokens_out": self._tout, "cost_usd": 0.0})

    def _decisions_text(self) -> str:
        try:
            lines = self.ws.decisions_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(ln for ln in lines if ln.startswith("- "))

    def _record_decision(self, text: str, source: str) -> None:
        self._decision_n += 1
        did = f"d{self._decision_n}"
        append_decision(self.ws, did, text, source)
        self._emit(T.DECISION, {"id": did, "text": text, "source": source})

    # ------------------------------------------------------------ models & profiles
    def _client(self, model: str) -> LLMClient:
        if model not in self._clients:
            if self.llm_factory is None:
                self._log("warn", "engine", f"No way to create a client for {model!r}; using {self.model!r} instead")
                return self.llm
            self._clients[model] = self.llm_factory(model)
        return self._clients[model]

    def _profile(self, model: str) -> Profile:
        return resolve(self.cfg.strength, self._model_sizes.get(model, ""), model)

    def _learn_models(self) -> None:
        lister = getattr(self.llm, "list_models_detailed", None)
        if lister is None:
            plain = getattr(self.llm, "list_models", None)
            try:
                self._installed = set(plain()) if plain else None
            except LLMError:
                self._installed = None
            return
        try:
            info = lister()
        except LLMError as e:
            self._log("warn", "engine", "Could not list the installed models", str(e))
            return
        self._installed = {m["name"] for m in info}
        for m in info:
            self._model_sizes[m["name"]] = m.get("parameter_size", "")
            if m["name"].endswith(":latest"):
                self._model_sizes[m["name"][: -len(":latest")]] = m.get("parameter_size", "")

    # ------------------------------------------------------------ safe points & waiting
    def _checkpoint(self, agent_id: str | None = None) -> None:
        """Safe point: honour stop and pause (agents report `paused` before the run does); answer the user."""
        if self._stop.is_set():
            raise StopRun()
        self._serve_inbox()
        self._serve_reports()
        if not self._pause_requested:
            return
        paused_agent = agent_id if agent_id and self._agent_state.get(agent_id) == "running" else None
        if paused_agent:
            self._set_agent(paused_agent, "paused")
        self._emit(T.RUN_PAUSED)
        self._wait(lambda: not self._pause_requested)
        self._emit(T.RUN_RESUMED)
        if paused_agent:
            self._set_agent(paused_agent, "running")

    def _wait(self, done: Callable[[], bool]) -> None:
        """Wait for the user (approval, a decision, resume) while still answering their messages."""
        while not done():
            if self._stop.is_set():
                raise StopRun()
            self._serve_inbox()
            self._wake.wait(0.2)
            self._wake.clear()
        if self._stop.is_set():
            raise StopRun()

    # ------------------------------------------------------------ reports to the lead, handled at once
    def _report(self, task: Task, aid: str, text: str) -> None:
        """An agent's note_to_lead: like telling the project manager. The lead answers at the agent's next step."""
        self._emit(T.AGENT_MESSAGE, {"role": "agent", "text": f"[to the lead] {text}"}, agent_id=aid)
        self._log("info", "report", f"{aid} reported to the lead", text, task_id=task.id, agent_id=aid)
        self._reports.setdefault(task.id, []).append(text)
        who = f"the {task.role} agent working on {task.id} \u201c{task.title}\u201d (it owns {', '.join(task.owns) or 'nothing'})"
        self._escalations.append((task.id, aid, who, text))

    def _serve_reports(self) -> None:
        if self._serving_reports or self._plan is None:
            return
        self._serving_reports = True          # a plan review below passes safe points: never handle reports nested
        try:
            while self._escalations:
                tid, aid, who, text = self._escalations.pop(0)
                self._lead_reacts(tid, aid, who, text)
        finally:
            self._serving_reports = False

    def _lead_reacts(self, tid: str | None, aid: str | None, who: str, text: str) -> str:
        """The lead answers a report right away: a reply to the agent, a shared decision, and/or a plan change."""
        assert self._plan is not None
        if aid:
            self._line("note_received", agent=next((t.role for t in self._plan.tasks if t.id == tid), aid))
        ans = handle_note(self.llm, self.prompt, self._plan, self._status, who, text, self._decisions_text(), self.ws,
                          retries=self.cfg.review_retries)
        self._usage(ans.tokens_in, ans.tokens_out)
        self._log("info" if not ans.error else "warn", "lead", f"Handled a report from {who}: "
                  + ("plan change" if ans.replan else "answered"),
                  f"Report: {text}\n\nReply: {ans.reply}\nDecision: {ans.decision}\nReason: {ans.reason}"
                  + (f"\nError: {ans.error}" if ans.error else ""), task_id=tid)
        if ans.reply:
            self._say(ans.reply)
            if aid:
                self._lead_msgs.setdefault(aid, []).append(ans.reply)
        if ans.decision:
            self._record_decision(ans.decision, "orchestrator")
        if ans.replan and aid:
            self._lead_instructions.append(f"Report from {who}: {text}" + (f" (you answered: {ans.reply})" if ans.reply else ""))
            if self.cfg.review:
                self._review()
        return ans.reply or ans.reason

    # ------------------------------------------------------------ integration: do the parts work together?
    def _owners(self, path: str) -> list[str]:
        assert self._plan is not None
        return [t.id for t in self._plan.tasks if path_allowed(path, t.owns)]

    def _integration(self, task: Task | None) -> tuple[list[Problem], list[Problem]]:
        """Run the project. Problems inside `task`'s own files go back to its agent; the rest are between agents."""
        report = check_integration(self.ws.root)
        detail = "\n".join(f"- [{p.kind}] {p.message} (files: {', '.join(p.files)})" for p in report.problems)
        self._log("info" if not report.problems else "warn", "integration", report.summary(),
                  "\n".join(filter(None, [detail, *report.notes])) or None, task_id=task.id if task else None)
        own, cross = [], []
        for prob in report.problems:
            owners = {o for f in prob.files if f for o in self._owners(f)}
            (own if task is not None and owners <= {task.id} else cross).append(prob)
        return own, cross

    def _add_fix_task(self, problems: list[Problem], after: str | None) -> bool:
        """A task owning exactly the files involved, right after `after`, so the next agent fixes the seam."""
        assert self._plan is not None
        if self._fixes >= self.cfg.integration_fixes:
            return False
        files = sorted({f for p in problems for f in p.files if f and self._owners(f)})
        if not files:
            return False
        listing = "\n".join(f"- {p.message}" for p in problems)
        guidance = self._lead_reacts(None, None, "the integration check (it started the server, loaded the pages and "
                                     "called the API like a browser)", listing)
        self._fixes += 1
        owners = sorted({o for f in files for o in self._owners(f)})
        fix = Task(id=f"fix{self._fixes}", title="Make the parts work together", role="integrator",
                   goal="The automatic integration check ran the project like a browser and found:\n" + listing
                   + (f"\n\nThe lead says: {guidance}" if guidance else "")
                   + "\nChange only what is needed so that every part works with the others.",
                   depends_on=tuple(o for o in owners if self._status.get(o) != "skipped"), owns=tuple(files),
                   acceptance=tuple(["The integration check finds none of these problems any more."]
                                    + [p.message[:240] for p in problems[:3]]))
        tasks: list[Task] = []
        for t in self._plan.tasks:
            if self._status.get(t.id, "pending") == "pending" and set(t.depends_on) & set(owners):
                t = Task(t.id, t.title, t.role, t.goal, t.depends_on + (fix.id,), t.owns, t.acceptance)
            tasks.append(t)
            if t.id == after:
                tasks.append(fix)
        if fix not in tasks:
            tasks.append(fix)
        try:
            new = Plan(self._plan.version + 1, tuple(tasks))
            errors = validate_replan(self._plan, new, self._status)
        except PlanError as e:
            errors = [str(e)]
        if errors:
            self._log("warn", "integration", "Could not add a fix task; the lead's review will handle the problems",
                      "\n".join(errors))
            self._lead_instructions.append("The integration check found problems between agents:\n" + listing)
            return False
        self._line("integration_cross", n=len(problems))
        self._apply_update(new, "Integration check: " + ", ".join(sorted({p.kind for p in problems})))
        return True

    def _final_check(self) -> bool:
        """After the last task: the lead's final review and one more integration check. True if work was added."""
        assert self._plan is not None
        if self.cfg.review and self._lead_instructions and self._final_reviews < 2:
            self._final_reviews += 1
            self._review()
            if any(self._status[t.id] == "pending" for t in self._plan.tasks):
                return True
        if not self.cfg.integration:
            return False
        self._line("final_check")
        _, problems = self._integration(None)
        self._integration_left = problems
        if not problems:
            self._line("integration_ok")
            return False
        last = next((t.id for t in reversed(self._plan.tasks) if self._status.get(t.id) == "verified"), None)
        return self._add_fix_task(problems, last)

    # ------------------------------------------------------------ answering the user
    def _take_for_agent(self, agent_id: str) -> list[str]:
        with self._lock:
            mine = [t for tg, t in self._inbox if tg == agent_id]
            self._inbox = [(tg, t) for tg, t in self._inbox if tg != agent_id]
        return mine

    def _answer(self, agent_id: str, model: str, context: list[dict[str, Any]], text: str) -> str | None:
        """Reply in the agent's voice right away. Returns the instruction carried by the message, if any."""
        ans = answer_user(self._client(model), context, text)
        self._usage(ans.tokens_in, ans.tokens_out)
        if ans.failed:
            self._log("warn", "chat", "Could not generate a reply; the message is treated as an instruction",
                      agent_id=agent_id)
        self._say(ans.reply or self._s("reply_failed"), agent_id)
        if ans.instruction:
            self._record_decision(ans.instruction, "user")
        return ans.instruction

    def _reply_as_running(self, aid: str, model: str, messages: list[dict[str, Any]], text: str) -> str | None:
        return self._answer(aid, model, messages, text)

    def _serve_inbox(self) -> None:
        """Answer messages for the lead and for agents that are not working right now."""
        with self._lock:
            take = [(tg, t) for tg, t in self._inbox if tg != self._running_agent]
            self._inbox = [(tg, t) for tg, t in self._inbox if tg == self._running_agent]
        for target, text in take:
            if target == ORCHESTRATOR_ID:
                ins = self._answer(ORCHESTRATOR_ID, self.model, lead_context(self.prompt, self._plan, self._status), text)
                if ins:
                    self._lead_instructions.append(ins)
                continue
            tid = next((k for k, v in self._agent_for.items() if v == target), None)
            if tid is None or self._plan is None:
                continue
            task = self._plan.by_id()[tid]
            model = self._agent_model.get(target, self.model)
            context = [{"role": "system", "content": build_worker_system(task, self._plan, self.ws, self._decisions_text(), self._profile(model))}]
            if tid in self._results:
                context.append({"role": "assistant", "content": f"I finished my task: {self._results[tid]}"})
            ins = self._answer(target, model, context, text)
            if ins:
                if self._agent_state.get(target) == "queued":
                    self._pending_for_agent.setdefault(target, []).append(ins)   # it starts with this in mind
                else:
                    self._lead_instructions.append(f"(for {task.title}) {ins}")    # its work is done: the lead decides

    # --------------------------------------------------------------------- the run
    def _main(self) -> None:
        try:
            self._run()
        except StopRun:
            self._end("stopped", self._s("stopped"))
        except Exception as e:  # a bug must surface in the UI, never vanish in a thread
            import traceback
            self._emit(T.ERROR, {"message": f"{type(e).__name__}: {e}"})
            self._log("error", "engine", f"Unexpected engine error: {type(e).__name__}: {e}", traceback.format_exc())
            self._end("failed", f"{type(e).__name__}: {e}")

    def _settings(self) -> dict[str, Any]:
        prof = self._profile(self.model)
        return {"model": self.model, "strength": self.cfg.strength, "profile": prof.name,
                "parameter_size": self._model_sizes.get(self.model, ""), "context": self.cfg.context,
                "approval": self.cfg.require_approval, "on_failure": self.cfg.on_failure, "review": self.cfg.review}

    def _run(self) -> None:
        self._learn_models()
        inputs = [f"inputs/{p.name}" for p in sorted(self.ws.inputs_dir.glob("*"))] if self.ws.inputs_dir.exists() else []
        settings = self._settings()
        self._emit(T.RUN_STARTED, {"project_path": str(self.ws.root), "prompt": self.prompt, "inputs": inputs,
                                   "mock": False, "model": self.model, "settings": settings,
                                   **({"resumed": True} if self.carry else {})})
        prof = self._profile(self.model)
        self._log("info", "engine", f"Model {self.model} ({settings['parameter_size'] or 'size unknown'}), strength "
                  f"{self.cfg.strength} → {prof.describe()}, context: {self.cfg.context}")
        if self.carry:
            self._restore(self.carry)
        else:
            self._line("analyzing")
            self._checkpoint()
            try:
                plan, sr = make_plan(self.llm, self.prompt, input_files=inputs, retries=self.cfg.plan_retries,
                                     min_tasks=prof.min_tasks, max_tasks=prof.max_tasks)
            except (StructuredOutputError, LLMError) as e:
                msg = self._s("planning_failed", error=str(e)[:300])
                self._emit(T.ERROR, {"message": msg})
                self._log("error", "planner", "No valid plan", str(e))
                self._end("failed", msg)
                return
            self._usage(sr.tokens_in, sr.tokens_out)
            if sr.attempts > 1:
                self._log("info", "planner", f"The plan needed {sr.attempts} attempts to pass validation")
            self._plan = plan
            self._status = {t.id: "pending" for t in plan.tasks}
            save_plan(self.ws, plan)
            self._emit(T.PLAN_CREATED, self._plan_payload(plan))
            for t in plan.tasks:
                self._create_agent(t)
            self._say(self._s("plan_ready", n=len(plan.tasks), first=plan.tasks[0].title))
            if self.cfg.require_approval:
                self._await_approval()
            if self._lead_instructions and self.cfg.review:
                self._review()          # the user asked for changes while reviewing the plan
        self._schedule()

    def _restore(self, carry: Carry) -> None:
        """Continuation run: show what was already done, put everything else back in the queue."""
        self._plan = carry.plan
        self._results, self._written = dict(carry.results), {k: list(v) for k, v in carry.written.items()}
        self._emit(T.PLAN_CREATED, self._plan_payload(carry.plan))
        redo = 0
        for t in carry.plan.tasks:
            aid = f"a_{t.id}"
            self._create_agent(t, carry.agent_models.get(aid, self.model))
            if carry.statuses.get(t.id) == "verified":
                for st in ("running", "done", "verified"):
                    self._set_task(t.id, st, carried=True)
                self._set_agent(aid, "running")
                self._set_agent(aid, "done")
            else:
                self._status[t.id] = "pending"
                redo += 1
        self._say(self._s("continuing", n=redo))

    def _await_approval(self) -> None:
        self._awaiting_approval = True
        self._approved = None
        self._emit(T.APPROVAL_REQUESTED, {"agents": dict(self._agent_model),
                                          "models": sorted(self._installed) if self._installed else [self.model],
                                          "sizes": dict(self._model_sizes)})
        self._line("awaiting")
        self._wait(lambda: self._approved is not None)
        assert self._approved is not None
        for aid, m in self._approved.items():
            if m != self._agent_model.get(aid):
                self._log("info", "engine", f"{aid} will use {m} instead of {self._agent_model.get(aid)}", agent_id=aid)
                self._agent_model[aid] = m
                self._emit(T.AGENT_UPDATED, {"model": m}, agent_id=aid)
        self._awaiting_approval = False
        self._emit(T.APPROVAL_GRANTED, {"models": dict(self._agent_model)})
        self._line("approved")

    @staticmethod
    def _plan_payload(plan: Plan) -> dict[str, Any]:
        return {"version": plan.version, "tasks": [t.to_dict() for t in plan.tasks], "stages": plan_stages(plan)}

    def _create_agent(self, task: Task, model: str | None = None) -> None:
        aid = f"a_{task.id}"
        model = model or self.model
        self._agent_for[task.id] = aid
        self._agent_model[aid] = model
        self._agents.add(aid)
        self._agent_state[aid] = "queued"
        title = task.role.replace("_", " ").replace("-", " ").title()
        self._emit(T.AGENT_CREATED, {"role": task.role, "model": model, "title": title, "task_ids": [task.id]}, agent_id=aid)

    # ----------------------------------------------------------------- scheduling
    def _schedule(self) -> None:
        assert self._plan is not None
        while True:
            self._checkpoint()
            pending = [t for t in self._plan.tasks if self._status[t.id] == "pending"]
            if not pending:
                if self._final_check():
                    continue
                break
            ready = [t for t in pending if all(self._status.get(d) == "verified" for d in t.depends_on)]
            if not ready:                      # everything left depends on failed/skipped work
                for t in pending:
                    self._line("skipped", title=t.title)
                    self._set_task(t.id, "skipped")
                break
            self._execute(ready[0])
            cross, self._cross = self._cross, []
            if cross and self._status.get(ready[0].id) == "verified":
                self._add_fix_task(cross, ready[0].id)
            if self.cfg.review and any(self._status[t.id] == "pending" for t in self._plan.tasks):
                self._review()
        total = len(self._plan.tasks)
        bad = sum(1 for t in self._plan.tasks if self._status[t.id] != "verified")
        msg = self._s("done_failed", bad=bad, n=total) if bad else \
            self._s("done_integration", n=len(self._integration_left)) if self._integration_left else self._s("done_ok")
        bad = bad or len(self._integration_left)
        self._say(msg)
        self._end("failed" if bad else "completed", msg)

    def _execute(self, task: Task) -> None:
        aid = self._agent_for[task.id]
        prof = self._profile(self._agent_model.get(aid, self.model))
        rescues = self.cfg.recoveries if self.cfg.recoveries is not None else prof.recoveries
        guidance: str | None = None
        while True:
            if self._attempts(task, aid, guidance):
                return
            diag = self._diagnose(task)
            if rescues > 0 and diag.action == "retry" and diag.guidance:
                rescues -= 1
                guidance = diag.guidance
                self._line("rescue_retry", title=task.title)
                self._log("info", "lead", f"Rescue: retrying {task.id} with new guidance", diag.guidance, task_id=task.id)
                continue
            if rescues > 0 and diag.action == "split" and diag.plan is not None:
                self._line("rescue_split", title=task.title)
                self._log("info", "lead", f"Rescue: replacing {task.id} with smaller tasks",
                          ", ".join(t.id for t in diag.plan.tasks), task_id=task.id)
                self._apply_update(diag.plan, diag.user_summary or diag.diagnosis, replaced=task.id)
                return
            decision, note = self._ask_user(task, diag)
            if decision == "retry":
                self._line("user_retry", title=task.title)
                guidance = "\n".join(x for x in (f"The user says: {note}" if note else "", diag.guidance) if x) or None
                rescues = self.cfg.recoveries if self.cfg.recoveries is not None else prof.recoveries
                continue
            if decision == "skip":
                self._line("user_skip", title=task.title)
                self._set_task(task.id, "skipped")
                return
            raise StopRun()

    def _attempts(self, task: Task, aid: str, guidance: str | None) -> bool:
        """Up to `max_attempts` worker attempts, each one told exactly why the previous one was rejected."""
        assert self._plan is not None
        model = self._agent_model.get(aid, self.model)
        prof = self._profile(model)
        client = self._client(model)
        max_attempts = self.cfg.max_attempts or prof.max_attempts
        feedback: list[str] | None = None
        history = self._history.setdefault(task.id, [])
        for attempt in range(1, max_attempts + 1):
            self._checkpoint()
            self._line("starting" if attempt == 1 and guidance is None else "retrying", title=task.title)
            self._set_task(task.id, "running")
            self._set_agent(aid, "running")
            box = ToolBox(self.ws, task.owns, self.log, aid, chunk_delay_cap=self.cfg.chunk_delay_cap,
                          emit=lambda type_, data, _a=aid: self._emit(type_, data, agent_id=_a), read_chars=prof.read_chars,
                          on_note=lambda text, _t=task, _a=aid: self._report(_t, _a, text))
            box.written = list(self._written.get(task.id, []))      # files from earlier attempts still count
            system = build_worker_system(task, self._plan, self.ws, self._decisions_text(), prof)
            user = build_worker_user(feedback, box.written, self._pending_for_agent.pop(aid, []), guidance)
            limits = WorkerLimits(max_steps=self.cfg.max_steps or prof.max_steps, temperature=prof.temperature,
                                  backoff=self.cfg.backoff)
            self._running_agent = aid
            words = self._last_words.setdefault(task.id, [])

            def said(text: str, _a=aid, _w=words) -> None:
                _w.append(text[:600])
                del _w[:-4]
                self._emit(T.AGENT_MESSAGE, {"role": "agent", "text": text[:4000]}, agent_id=_a)

            try:
                result = run_worker(
                    client, box, system, user,
                    gate=lambda _a=aid: self._checkpoint(_a),
                    deliver=lambda _a=aid: self._take_for_agent(_a),
                    reply=lambda msgs, text, _a=aid, _m=model: self._reply_as_running(_a, _m, msgs, text),
                    on_message=said, on_usage=self._usage,
                    log=lambda level, source, message, detail=None, _a=aid, _t=task.id: self._log(level, source, message, detail, task_id=_t, agent_id=_a),
                    limits=limits, sleep=self._sleep, deliver_lead=lambda _a=aid: self._lead_msgs.pop(_a, []))
            finally:
                self._running_agent = None
            self._written[task.id] = box.written
            self._notes.extend((task.id, n) for n in box.notes)
            problems = check_task(self.ws, task, box.written) if result.ok else [result.error or "the agent failed"]
            kind = "" if not problems else (result.kind or "checks") if not result.ok else "checks"
            if not problems and self.cfg.integration:
                own, self._cross = self._integration(task)
                if own:
                    self._line("integration_own", title=task.title)
                    problems, kind = [f"integration check: {p.message}" for p in own], "integration"
            if not problems:
                self._results[task.id] = result.summary
                self._set_task(task.id, "done")
                self._set_agent(aid, "done")
                self._set_task(task.id, "verified")
                return True
            history.append({"kind": kind, "problems": problems})
            feedback = problems
            self._emit(T.ERROR, {"message": self._s("task_failed", title=task.title, problems="; ".join(problems)[:600])},
                       agent_id=aid, task_id=task.id)
            self._log("error", "task", f"{task.id} attempt {attempt}/{max_attempts} failed ({kind}): {'; '.join(problems)[:300]}",
                      "Problems:\n- " + "\n- ".join(problems) + "\n\nAgent's last words:\n" + "\n---\n".join(words),
                      task_id=task.id, agent_id=aid)
            self._set_task(task.id, "failed")
            self._set_agent(aid, "error")
            if kind in INFRA_KINDS:
                break      # the model server is failing: more attempts right now would fail the same way
        return False

    def _diagnose(self, task: Task):
        assert self._plan is not None
        self._line("diagnosing", title=task.title)
        diag = diagnose_failure(self.llm, self.prompt, task, self._plan, self._status, self._history.get(task.id, []),
                                self._written.get(task.id, []),
                                [n for t, n in self._notes if t == task.id] + self._reports.get(task.id, []),
                                "\n---\n".join(self._last_words.get(task.id, [])), self.ws, retries=self.cfg.review_retries)
        self._usage(diag.tokens_in, diag.tokens_out)
        self._log("info" if not diag.error else "warn", "lead", f"Diagnosis for {task.id}: {diag.action}",
                  f"{diag.diagnosis}\n\nFor the user: {diag.user_summary}\nGuidance: {diag.guidance}\n"
                  f"Suggestions: {'; '.join(diag.suggestions)}" + (f"\nError: {diag.error}" if diag.error else ""), task_id=task.id)
        return diag

    def _ask_user(self, task: Task, diag) -> tuple[str, str]:
        policy = self.cfg.on_failure
        if policy in ("skip", "stop"):
            self._log("info", "engine", f"{task.id} could not be rescued; policy says {policy}", task_id=task.id)
            return policy, ""
        reasons: list[str] = []
        for a in self._history.get(task.id, []):
            for p in a.get("problems", []):
                if p not in reasons:
                    reasons.append(p)
        self._decision = None
        self._attention = task.id
        self._emit(T.ATTENTION_REQUIRED, {"title": task.title, "summary": diag.user_summary or diag.diagnosis,
                                          "diagnosis": diag.diagnosis, "reasons": reasons[-6:],
                                          "suggestions": diag.suggestions, "options": list(RESOLUTIONS)}, task_id=task.id)
        self._line("needs_user", title=task.title)
        self._wait(lambda: self._decision is not None)
        decision, note = self._decision or ("stop", "")
        self._attention = None
        self._emit(T.ATTENTION_RESOLVED, {"decision": decision, **({"note": note} if note else {})}, task_id=task.id)
        self._log("info", "user", f"Decision for {task.id}: {decision}" + (f" — {note}" if note else ""), task_id=task.id)
        if note:
            self._record_decision(f"For “{task.title}”: {note}", "user")
        return decision, note

    # ------------------------------------------------------------------- reviews
    def _review(self) -> None:
        assert self._plan is not None
        msgs, self._lead_instructions = self._lead_instructions, []
        notes, self._notes = self._notes, []
        self._checkpoint()
        self._line("reviewing")
        res = review_plan(self.llm, self.prompt, self._plan, self._status, self._results, self._written,
                          self._decisions_text(), notes, msgs, self.ws, retries=self.cfg.review_retries)
        self._usage(res.tokens_in, res.tokens_out)
        if res.error:
            self._line("review_failed")
            self._log("warn", "review", "Plan review failed; keeping the plan", res.error)
            return
        for d in res.decisions:
            self._record_decision(d, "orchestrator")
        if res.action == "update" and res.plan is not None:
            self._apply_update(res.plan, res.reason)
        else:
            self._line("plan_kept")

    def _apply_update(self, new: Plan, reason: str, replaced: str | None = None) -> None:
        assert self._plan is not None
        old = self._plan
        diff = diff_plans(old, new)
        self._plan = new
        for tid in diff["removed"]:
            self._status.pop(tid, None)
        for tid in diff["added"]:
            self._status[tid] = "pending"
        save_plan(self.ws, new)
        self._emit(T.PLAN_UPDATED, {**self._plan_payload(new), "reason": reason, **diff})
        self._log("info", "lead", f"Plan v{new.version}: +{diff['added']} -{diff['removed']} ~{diff['changed']}", reason)
        for tid in diff["removed"]:
            aid = self._agent_for.pop(tid, None)
            if aid:
                self._agents.discard(aid)
                self._agent_state.pop(aid, None)
                self._agent_model.pop(aid, None)
                with self._lock:
                    self._inbox = [(tg, t) for tg, t in self._inbox if tg != aid]
                self._emit(T.AGENT_REMOVED, {}, agent_id=aid)
        for t in new.tasks:
            if t.id in diff["added"]:
                self._create_agent(t)
        self._say(self._s("plan_updated", reason=reason))
