"""The lead agent's thinking: how to brief a worker, and how to REVIEW the plan while the run is in progress.

After each task the lead looks at what actually happened (results, files, worker notes, user messages) and may
adjust the REMAINING plan. The model answers with a structured object; `validate_replan` guarantees it cannot
rewrite history (running/finished tasks are frozen) or break the plan (cycles, parallel file conflicts)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .llm import LLMClient, LLMError, StructuredOutputError, generate_structured
from .plan import Plan, PlanError, Task, plan_json_schema, replan_json_schema, validate_replan
from .profiles import PROFILES, Profile
from .workspace import Workspace

MAX_CONTRACT_CHARS = 8000
MAX_CONTRACT_FILES = 6


# ------------------------------------------------------------------ briefing a worker

def project_tree(ws: Workspace, limit: int = 120) -> str:
    out: list[str] = []
    for p in sorted(ws.root.rglob("*")):
        rel = p.relative_to(ws.root)
        if any(part.startswith(".") or part in ("node_modules", "__pycache__") for part in rel.parts):
            continue
        if p.is_file():
            out.append(rel.as_posix())
        if len(out) >= limit:
            out.append("...")
            break
    return "\n".join(out) if out else "(empty - nothing has been created yet)"


def contracts_text(ws: Workspace) -> str:
    folder = ws.root / "contracts"
    if not folder.is_dir():
        return ""
    chunks = []
    for p in sorted(folder.rglob("*"))[:MAX_CONTRACT_FILES]:
        if p.is_file():
            body = p.read_bytes()[:MAX_CONTRACT_CHARS].decode("utf-8", errors="replace")
            chunks.append(f"--- {p.relative_to(ws.root).as_posix()} ---\n{body}")
    return "\n\n".join(chunks)


WORKER_EXAMPLE = """Example of a good sequence of calls (adapt it to your task):
1. list_files {}
2. read_file {"path": "contracts/api.md"}
3. write_file {"path": "backend/server.js", "content": "<the first ~150 lines>"}
4. append_file {"path": "backend/server.js", "content": "<the next part>"}
5. write_file {"path": "backend/package.json", "content": "<complete file>"}
6. check_project {}
7. finish {"summary": "Built the server with the two endpoints from the contract."}"""


def _team(task: Task, plan: Plan, statuses: Mapping[str, str]) -> str:
    """Who builds what, and when. Shown only the other tasks' titles, a backend agent built the pages itself inside
    backend/ (live run cafe-35b-v4): it did not know that a frontend agent would build them after it."""
    out = []
    for t in plan.tasks:
        if t.id == task.id:
            continue
        st = statuses.get(t.id, "pending")
        when = {"done": "finished", "verified": "finished", "running": "working now",
                "pending": "starts after you"}.get(st, st)
        out.append(f"- {t.id} [{t.role}] {t.title} - builds {', '.join(t.owns) or '(nothing, read-only)'} - {when}")
    return "\n".join(out) or "(none)"


def build_worker_system(task: Task, plan: Plan, ws: Workspace, decisions: str, profile: Profile | None = None,
                        statuses: Mapping[str, str] | None = None) -> str:
    p = profile or PROFILES["medium"]
    owns = ", ".join(task.owns) if task.owns else "NOTHING (this task is read-only; do not call write_file)"
    others = _team(task, plan, statuses or {})
    contracts = contracts_text(ws)
    inputs = sorted(q.name for q in ws.inputs_dir.glob("*")) if ws.inputs_dir.exists() else []
    return f"""You are the "{task.role}" agent on a software team. Work ONLY on your own task. You can do this well.
TASK_ID: {task.id}

Task: {task.title}
Goal: {task.goal}
Acceptance criteria (all must be true when you finish):
{chr(10).join('- ' + a for a in task.acceptance)}

You may create or modify files ONLY inside these paths: {owns}
Tools: list_files, read_file, write_file, append_file, note_to_lead, check_project, finish.

Rules:
1. Look at what already exists before you start, and follow the shared contracts exactly.
2. Write COMPLETE, working files. No placeholders, no "TODO", no "...".
3. Long files: write_file the first part (about 150 lines), then append_file the rest in parts. Keep every call short enough.
4. You cannot run programs, tests or shell commands here, so do not try and do not write scripts just to run things.
   But you CAN call check_project: the engine then starts the project's server, loads every page with its
   scripts/styles/images and calls the API the way a browser does, and tells you what is broken. Use it after you
   change a server or pages and before finish: reading code is not proof that it works. The same check runs
   automatically after you finish.
5. When a tool returns ERROR, read why and change your approach. Never repeat a call that failed or changed nothing.
6. If your work needs a change in a file you do not own (for example linking your stylesheet from index.html, or the
   backend must serve your pages / allow your requests), call note_to_lead AT ONCE with the exact change, like telling
   your project manager. The lead answers within a step or two; meanwhile continue with your own files. Never work
   around it and never assume the other side will "probably" match.
7. The other agents' files (see "The team" below) are built by them, also when they do not exist yet because that
   agent starts after you. Never create them yourself, not even a copy or a stand-in inside your own folder (for
   example pages inside backend/): build your side so that it works with theirs as the contract describes.
8. Use relative paths from the project root, with forward slashes.
9. As soon as every acceptance criterion is met, call finish with a short summary. Do not keep polishing.
{chr(10) + WORKER_EXAMPLE + chr(10) if p.detailed else ''}
Files in the project now:
{project_tree(ws)}
{('Files provided by the user (read them with read_file): ' + ', '.join('inputs/' + n for n in inputs)) if inputs else ''}

Shared contracts (written by earlier tasks):
{contracts or '(none yet)'}

Decisions made so far (follow them):
{decisions.strip() or '(none yet)'}

The team (who builds what; do not do their work):
{others}"""


def build_worker_user(feedback: Sequence[str] | None, written: Sequence[str], user_messages: Sequence[str],
                      guidance: str | None = None) -> str:
    parts = ["Begin your task."]
    if guidance:
        parts.append("Guidance from the lead agent, who studied why earlier attempts failed - follow it:\n" + guidance)
    if user_messages:
        parts.append("Messages from the user for you:\n" + "\n".join(f"- {m}" for m in user_messages))
    if feedback:
        parts.append("Your previous attempt was rejected for these reasons:\n" + "\n".join(f"- {f}" for f in feedback)
                     + "\nFix every problem. Files you already wrote: " + (", ".join(written) or "none") + ".")
    return "\n\n".join(parts)


# ------------------------------------------------------------------- reviewing the plan

REVIEW_SYSTEM = """You are the lead orchestrator of a team of AI software agents, reviewing progress in the middle of a run.
Decide whether the REMAINING plan should change, given what really happened.

Answer ONLY with JSON: {"action": "keep" | "update", "reason": "...", "decisions": [...], "tasks": [...]}.

- "keep": the plan is fine. Put an empty list in "tasks".
- "update": return the COMPLETE new list of tasks (same fields as before: id, title, role, goal, depends_on, owns, acceptance).

Hard rules for an update:
1. Tasks whose status is running, done, verified or failed are history: copy them EXACTLY as given, unchanged.
2. You may add, remove or edit tasks whose status is pending.
3. Keep task ids stable. Dependencies must exist and must not form a cycle.
4. Two tasks that can run at the same time must not own overlapping paths.
5. Only change the plan when there is a concrete reason (a worker note, a user message, a failed or surprising result,
   missing work discovered). Do not rewrite the plan for style. If unsure, answer "keep".
6. Act on worker notes. If a note asks for a change in a file that another task owns and that task is already done,
   add a small pending follow-up task that owns exactly that file and depends on the tasks it needs.
7. "decisions" lists any new shared decisions every agent must follow (short sentences); otherwise [].
8. Write reason, titles and goals in the same language as the original request.
9. "Integration check" below is EVIDENCE: the engine really started the project and loaded it like a browser. A
   result_summary is only an agent's CLAIM. When they disagree, the evidence wins: never call a fix done or a task
   redundant while the integration check still reports the problem."""


def build_review_user(
    prompt: str, plan: Plan, statuses: Mapping[str, str], results: Mapping[str, str], written: Mapping[str, Sequence[str]],
    decisions: str, notes: Sequence[tuple[str, str]], user_messages: Sequence[str], ws: Workspace,
    evidence: str = "",
) -> str:
    tasks = []
    for t in plan.tasks:
        d = t.to_dict()
        d["status"] = statuses.get(t.id, "pending")
        if t.id in results:
            d["result_summary"] = results[t.id]
        if written.get(t.id):
            d["files_written"] = list(written[t.id])
        tasks.append(d)
    sections = [f"Original request:\n{prompt}", "Current plan with statuses:\n" + json.dumps(tasks, ensure_ascii=False, indent=1),
                "Files in the project:\n" + project_tree(ws)]
    if decisions.strip():
        sections.append("Decisions so far:\n" + decisions.strip())
    if notes:
        sections.append("Notes from workers:\n" + "\n".join(f"- [{tid}] {text}" for tid, text in notes))
    if user_messages:
        sections.append("Messages from the user (treat them as instructions):\n" + "\n".join(f"- {m}" for m in user_messages))
    if evidence:
        sections.append("Integration check (the engine ran the project just now; evidence, not a claim):\n" + evidence)
    return "\n\n".join(sections)


@dataclass
class ReviewResult:
    action: str                # "keep" | "update"
    reason: str
    decisions: list[str]
    plan: Plan | None          # the validated new plan when action == "update"
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None   # set when the review itself failed (the plan is then simply kept)


def review_plan(llm: LLMClient, prompt: str, plan: Plan, statuses: Mapping[str, str], results: Mapping[str, str],
                written: Mapping[str, Sequence[str]], decisions: str, notes: Sequence[tuple[str, str]],
                user_messages: Sequence[str], ws: Workspace, *, retries: int = 2, evidence: str = "",
                protected: Mapping[str, str] | None = None) -> ReviewResult:
    def validate(obj: Any) -> list[str]:
        if not isinstance(obj, dict):
            return ["top-level JSON must be an object"]
        if obj.get("action") == "keep":
            return []
        if obj.get("action") != "update":
            return ['"action" must be "keep" or "update"']
        try:
            new = Plan.from_dict({"version": plan.version + 1, "tasks": obj.get("tasks")})
        except PlanError as e:
            return [str(e)]
        return validate_replan(plan, new, statuses, protected=protected)

    messages = [{"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": build_review_user(prompt, plan, statuses, results, written, decisions, notes,
                                                              user_messages, ws, evidence)}]
    try:
        res = generate_structured(llm, messages, schema=replan_json_schema(), validate=validate, retries=retries)
    except StructuredOutputError as e:
        return ReviewResult("keep", "", [], None, error="; ".join(e.errors[:3]))
    except Exception as e:  # LLMError etc.: a failed review must never kill the run
        return ReviewResult("keep", "", [], None, error=str(e))
    obj = res.value
    decisions_out = [d.strip() for d in obj.get("decisions", []) if isinstance(d, str) and d.strip()]
    if obj["action"] == "keep":
        return ReviewResult("keep", str(obj.get("reason", "")), decisions_out, None, res.tokens_in, res.tokens_out)
    new_plan = Plan.from_dict({"version": plan.version + 1, "tasks": obj["tasks"]})
    old_tasks = {t.id: t for t in plan.tasks}
    if {t.id: t for t in new_plan.tasks} == old_tasks:           # "update" that changes nothing
        return ReviewResult("keep", str(obj.get("reason", "")), decisions_out, None, res.tokens_in, res.tokens_out)
    return ReviewResult("update", str(obj.get("reason", "")).strip() or "Plan adjusted", decisions_out, new_plan,
                        res.tokens_in, res.tokens_out)


# ------------------------------------------------------------------- answering the user

REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reply": {"type": "string"}, "is_instruction": {"type": "boolean"}, "instruction": {"type": "string"}},
    "required": ["reply", "is_instruction", "instruction"],
    "additionalProperties": False,
}


@dataclass
class Answer:
    reply: str
    instruction: str | None     # None: a question or a comment, nothing to apply
    tokens_in: int = 0
    tokens_out: int = 0
    failed: bool = False


def answer_user(llm: LLMClient, context: list[dict[str, Any]], text: str) -> Answer:
    """Reply to the user right away, in the agent's own voice and with its own context, and tell an instruction
    (which changes the work and goes into the decision ledger) apart from a question (which only needs an answer)."""
    ask = (f"The user has just sent you this message while you work:\n\"{text}\"\n\n"
           "Reply to them now, directly, in the language they wrote in, in 1-3 short sentences: answer the question or say "
           "what you will do. Then classify the message. It is an instruction (true) when the user tells the team what to "
           "do or how the result must be: a requirement, preference, constraint or change (colors, wording, a feature, a "
           "port, a rule), EVEN IF it already fits the current plan. Your team only learns it from the decision you "
           "record, so when in doubt, it is an instruction. It is not an instruction (false) only when it is a question, "
           "thanks, or a remark that asks for nothing. If it is an instruction, restate it in one clear sentence in "
           "\"instruction\"; otherwise use \"\". "
           "Answer only with JSON: {\"reply\": \"...\", \"is_instruction\": true, \"instruction\": \"...\"}")
    # "an instruction that changes the work" was read as "changes the plan": twice in live runs the lead answered
    # "I'll fold this into the frontend build" and recorded nothing, so no agent heard of it.
    msgs = [dict(m) for m in context] + [{"role": "user", "content": ask}]

    def validate(obj: Any) -> list[str]:
        if not isinstance(obj, dict) or not str(obj.get("reply", "")).strip():
            return ['"reply" must be a non-empty string']
        return []

    try:
        res = generate_structured(llm, msgs, schema=REPLY_SCHEMA, validate=validate, retries=1, temperature=0.2)
    except (StructuredOutputError, LLMError):
        return Answer("", text, failed=True)   # could not answer: treat the message as an instruction, never drop it
    v = res.value
    # Either signal makes it an instruction: losing one costs far more than an extra line in the ledger. Live run
    # cafe-35b-v5: "Use a warm color palette ..." got the reply "I'll fold this into the frontend build" and no
    # decision, so no agent would have seen it (three repeats of the same call classified it correctly). Ollama
    # writes the keys in alphabetical order, so the restatement comes BEFORE is_instruction: it is the first signal.
    restated = str(v.get("instruction", "")).strip()
    instruction = restated or (text.strip() if v.get("is_instruction") else "")
    return Answer(str(v["reply"]).strip(), instruction or None, res.tokens_in, res.tokens_out)


def lead_context(prompt: str, plan: Plan | None, statuses: Mapping[str, str]) -> list[dict[str, Any]]:
    lines = [f"- {t.id} [{t.role}] {t.title}: {statuses.get(t.id, 'pending')}" for t in plan.tasks] if plan else ["(no plan yet)"]
    return [{"role": "system", "content": "You are the lead orchestrator of a team of AI software agents. You planned the work, "
             "hand it out, check the results and adjust the plan.\n\nOriginal request:\n" + prompt +
             "\n\nCurrent plan and status:\n" + "\n".join(lines)}]


# ------------------------------------------------------------------- a report from the team, handled at once

NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"reply": {"type": "string"}, "decision": {"type": "string"}, "replan": {"type": "boolean"},
                   "reason": {"type": "string"}},
    "required": ["reply", "decision", "replan", "reason"],
    "additionalProperties": False,
}

NOTE_SYSTEM = """You are the lead orchestrator of a team of AI software agents, like a project manager. A report has just
come in while the work is in progress: from one of your agents, or from the automatic integration check that runs the
project. Handle it NOW, the way a good lead does when a team member says "I need a change in someone else's part".

Answer ONLY with JSON: {"reply": "...", "decision": "...", "replan": true|false, "reason": "..."}
- "reply": 1-3 sentences to the agent who reported it (or "" for the integration check): what to do right now. Agents may
  only edit the files they own, so never ask them to change someone else's files.
- "decision": a short shared rule that every agent must follow from now on, when the report shows the team needs one
  (for example "The backend serves the HTML pages; the frontend calls the API with relative URLs like /api/menu"), or "".
- "replan": true when work outside the reporting agent's files must change (another agent's file, a missing piece, a
  wrong contract): you will then update the plan right away. false when the reply is enough.
- "reason": one sentence: why.
Write reply, decision and reason in the language of the original request."""


@dataclass
class NoteAnswer:
    reply: str
    decision: str
    replan: bool
    reason: str
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


def handle_note(llm: LLMClient, prompt: str, plan: Plan, statuses: Mapping[str, str], source: str, note: str,
                decisions: str, ws: Workspace, *, retries: int = 1) -> NoteAnswer:
    """`source` describes who reported it ("agent 'frontend' working on t3 …" or "the integration check")."""
    lines = [f"- {t.id} [{t.role}] {t.title}: {statuses.get(t.id, 'pending')}; owns {', '.join(t.owns) or 'nothing'}"
             for t in plan.tasks]
    user = (f"Original request:\n{prompt}\n\nPlan and status:\n" + "\n".join(lines) +
            f"\n\nFiles in the project:\n{project_tree(ws)}\n\nDecisions so far:\n{decisions.strip() or '(none)'}\n\n"
            f"Report from {source}:\n{note}")

    def validate(obj: Any) -> list[str]:
        return [] if isinstance(obj, dict) and isinstance(obj.get("replan"), bool) else ['"replan" must be true or false']

    try:
        res = generate_structured(llm, [{"role": "system", "content": NOTE_SYSTEM}, {"role": "user", "content": user}],
                                  schema=NOTE_SCHEMA, validate=validate, retries=retries, temperature=0.2)
    except (StructuredOutputError, LLMError) as e:
        # the lead could not think it through: never drop the report, let the plan review deal with it
        return NoteAnswer("", "", True, "the lead could not answer; the plan review will handle it", error=str(e))
    v = res.value
    return NoteAnswer(str(v.get("reply", "")).strip(), str(v.get("decision", "")).strip(), bool(v["replan"]),
                      str(v.get("reason", "")).strip(), res.tokens_in, res.tokens_out)


# ------------------------------------------------------------------- rescuing a failed task

# What the user can do about each kind of failure (shown in the report when the lead asks for a decision).
SUGGESTIONS: dict[str, list[str]] = {
    "truncated": ["Retry: the agent now writes long files in parts.",
                  "Give Ollama a bigger context window (start screen, Context) or leave it on the Ollama default.",
                  "Let the lead split the task into smaller ones (retry with a note asking for it)."],
    "loop": ["Retry with a note that says exactly what to do next.",
             "Skip it if the files it already wrote are enough."],
    "step_cap": ["Retry with a note that narrows the task down.", "Skip it if the files it already wrote are enough."],
    "no_work": ["Retry; if it keeps happening the model may be too weak for this task: pick a larger model for this agent, "
                "or set the strength to Small."],
    "model_unavailable": ["Check that Ollama is running and has enough GPU memory, then retry.",
                          "Try a smaller model or a smaller context window."],
    "model_fatal": ["Install the model in Ollama (ollama pull …) or choose another model, then retry."],
    "checks": ["Retry: the agent gets the exact errors from the checks.", "Retry with a note that explains the fix you want."],
}
INFRA_KINDS = ("model_unavailable", "model_fatal")


def diagnose_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "diagnosis": {"type": "string"},
            "user_summary": {"type": "string"},
            "suggestions": {"type": "array", "items": {"type": "string"}},
            "action": {"type": "string", "enum": ["retry", "split", "ask_user"]},
            "guidance": {"type": "string"},
            "tasks": plan_json_schema()["properties"]["tasks"],
        },
        "required": ["diagnosis", "user_summary", "suggestions", "action", "guidance", "tasks"],
        "additionalProperties": False,
    }


DIAGNOSE_SYSTEM = """You are the lead orchestrator. One of your agents has failed its task several times. You can rescue it.
Find the ROOT cause from the evidence below - not the symptom - and choose how to fix it.

Answer ONLY with JSON: {"diagnosis", "user_summary", "suggestions", "action", "guidance", "tasks"}.
- action "retry": a clearer instruction will let the agent succeed. Put concrete, step-by-step guidance in "guidance":
  which files to write, in what order, what to avoid, and how to write long files in parts. "tasks": [].
- action "split": the task is too big for one agent. Return the COMPLETE new task list in "tasks": the failed task replaced
  by 2-4 smaller tasks with NEW ids; every other task copied exactly as it is, except that tasks which depended on the
  failed task now depend on the new task(s) that finish its work. "guidance": "".
- action "ask_user": only if it cannot be solved without the user (missing information, contradicting requirements).
- "diagnosis": the root cause, for engineers. "user_summary": 1-2 plain sentences for the user, in the language of the
  original request. "suggestions": 2-4 short options for the user.
Agents cannot run programs or tests; do not suggest that they do."""


@dataclass
class Diagnosis:
    action: str                 # retry | split | ask_user
    diagnosis: str
    user_summary: str
    suggestions: list[str]
    guidance: str = ""
    plan: Plan | None = None    # the rescue plan when action == "split"
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


def diagnose_failure(llm: LLMClient, prompt: str, task: Task, plan: Plan, statuses: Mapping[str, str],
                     attempts: Sequence[dict[str, Any]], written: Sequence[str], notes: Sequence[str],
                     transcript_tail: str, ws: Workspace, *, retries: int = 2) -> Diagnosis:
    kinds = [a.get("kind") or "checks" for a in attempts]
    last_kind = kinds[-1] if kinds else "checks"
    rule_suggestions = SUGGESTIONS.get(last_kind, SUGGESTIONS["checks"])
    problems = [p for a in attempts for p in a.get("problems", [])]
    if last_kind in INFRA_KINDS:
        # the model server itself is failing: asking that same model for advice would be pointless
        return Diagnosis("ask_user", f"infrastructure failure ({last_kind}): {problems[-1] if problems else ''}",
                         "The model server kept failing while this task ran, so the agents could not work.",
                         rule_suggestions)

    def validate(obj: Any) -> list[str]:
        if not isinstance(obj, dict) or obj.get("action") not in ("retry", "split", "ask_user"):
            return ['"action" must be "retry", "split" or "ask_user"']
        if obj["action"] == "retry" and not str(obj.get("guidance", "")).strip():
            return ['"retry" needs concrete "guidance"']
        if obj["action"] == "split":
            try:
                new = Plan.from_dict({"version": plan.version + 1, "tasks": obj.get("tasks")})
            except PlanError as e:
                return [str(e)]
            if task.id in new.by_id():
                return [f"the failed task {task.id!r} must be replaced by new tasks with new ids"]
            return validate_replan(plan, new, statuses, replaceable={task.id})
        return []

    evidence = {
        "failed_task": task.to_dict(),
        "attempts": [{"attempt": i + 1, "kind": a.get("kind") or "checks", "problems": a.get("problems", [])} for i, a in enumerate(attempts)],
        "files_written_by_this_task": list(written),
        "notes_from_the_agent": list(notes),
        "plan": [{**t.to_dict(), "status": statuses.get(t.id, "pending")} for t in plan.tasks],
    }
    user = (f"Original request:\n{prompt}\n\nEvidence:\n{json.dumps(evidence, ensure_ascii=False, indent=1)}\n\n"
            f"Files in the project:\n{project_tree(ws)}\n\nThe agent's last words:\n{transcript_tail or '(none)'}")
    try:
        res = generate_structured(llm, [{"role": "system", "content": DIAGNOSE_SYSTEM}, {"role": "user", "content": user}],
                                  schema=diagnose_schema(), validate=validate, retries=retries, temperature=0.2)
    except (StructuredOutputError, LLMError) as e:
        return Diagnosis("ask_user", f"the lead could not produce a diagnosis: {e}",
                         "The task failed and the lead agent could not work out a fix on its own.", rule_suggestions, error=str(e))
    v = res.value
    suggestions = [x.strip() for x in v.get("suggestions", []) if isinstance(x, str) and x.strip()][:4] or rule_suggestions
    new_plan = Plan.from_dict({"version": plan.version + 1, "tasks": v["tasks"]}) if v["action"] == "split" else None
    return Diagnosis(v["action"], str(v.get("diagnosis", "")).strip(), str(v.get("user_summary", "")).strip(), suggestions,
                     str(v.get("guidance", "")).strip(), new_plan, res.tokens_in, res.tokens_out)
