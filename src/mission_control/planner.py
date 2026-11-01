"""Orchestrator planning step (Phase 0 version).

Prompt in -> validated `Plan` out. This is deliberately small: it exists now so we can
find out EARLY how well the chosen local model (ornith-1.5:9b) plans, via
`scripts/smoke_llm.py`. The full orchestrator (replanning, dispatch) arrives in Phase 2.
"""
from __future__ import annotations

from typing import Any, Sequence

from .llm import LLMClient, StructuredResult, generate_structured
from .plan import Plan, PlanError, plan_json_schema, validate_plan

PLANNER_SYSTEM = """You are the lead orchestrator of a team of AI software agents.
Turn the user's request into an execution plan made of tasks. Each task is carried out by ONE agent.

Return ONLY a JSON object of the form {"tasks": [ ... ]}. Each task has:
  id          lowercase letters/digits/_/-, starting with a letter (e.g. "t1", "backend")
  title       short human-readable title
  role        short English role name (e.g. "architect", "backend", "frontend", "qa")
  goal        1-2 sentences: what this agent must produce
  depends_on  ids of tasks that must be finished first (no cycles; use [] if none)
  owns        files/folders this task alone may write, relative to the project root
              (globs allowed, e.g. "backend/**"); [] for read-only tasks
  acceptance  1-4 concrete, checkable statements that prove the task is done

Rules:
1. Use between {min_tasks} and {max_tasks} tasks. Prefer fewer, well-scoped tasks that one agent can finish.
2. Two tasks that can run at the same time (neither depends on the other) must NOT own
   overlapping paths. If they would overlap, make one depend on the other.
3. If several tasks must agree on an interface (for example frontend and backend), create
   a FIRST task that writes that interface/contract under "contracts/", and make the
   dependent tasks depend on it.
4. Never use ".mission" in owns.
5. Write title and goal in the same language as the user's request. Keep id and role in English.
"""


def build_planner_messages(prompt: str, input_files: Sequence[str] = (), min_tasks: int = 3, max_tasks: int = 8) -> list[dict[str, Any]]:
    user = f"User request:\n{prompt.strip()}\n"
    if input_files:
        user += "\nFiles provided by the user (available under inputs/):\n" + "\n".join(f"- {f}" for f in input_files)
    system = PLANNER_SYSTEM.replace("{min_tasks}", str(min_tasks)).replace("{max_tasks}", str(max_tasks))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _validate_llm_plan(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return ["top-level JSON must be an object like {\"tasks\": [...]}"]
    try:
        plan = Plan.from_dict({"version": 1, "tasks": obj.get("tasks")})
    except PlanError as e:
        return [str(e)]
    return validate_plan(plan)


def make_plan(
    llm: LLMClient, prompt: str, *, input_files: Sequence[str] = (), retries: int = 2, min_tasks: int = 3, max_tasks: int = 8
) -> tuple[Plan, StructuredResult]:
    """Raises StructuredOutputError if the model cannot produce a valid plan within the retries."""
    result = generate_structured(
        llm,
        build_planner_messages(prompt, input_files, min_tasks, max_tasks),
        schema=plan_json_schema(),
        validate=_validate_llm_plan,
        retries=retries,
    )
    plan = Plan.from_dict({"version": 1, "tasks": result.value["tasks"]})
    return plan, result
