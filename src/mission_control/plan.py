"""Plan model + validation.

The orchestrator's output is a *structured plan* (a DAG of tasks), never free text.
`validate_plan` is the gatekeeper: a plan that fails here is never executed, and the
error list is fed back to the planner LLM so it can repair its own output.

Rules enforced:
  * ids are unique and well-formed, dependencies exist, no cycles
  * every task has at least one acceptance criterion (precision starts before the work)
  * `owns` paths are safe relative paths
  * two tasks that CAN run concurrently (neither depends on the other, directly or
    transitively) must not own overlapping paths  -> no write conflicts by construction
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_TASKS = 12
GLOB_CHARS = "*?["


class PlanError(ValueError):
    """Raised for structurally malformed plan data (wrong types / missing fields)."""


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(x, str) for x in value):
        raise PlanError(f"{name} must be a list of strings")
    return tuple(x.strip() for x in value)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    role: str
    goal: str
    depends_on: tuple[str, ...] = ()
    owns: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "role": self.role, "goal": self.goal,
            "depends_on": list(self.depends_on), "owns": list(self.owns),
            "acceptance": list(self.acceptance),
        }

    @classmethod
    def from_dict(cls, d: Any) -> "Task":
        if not isinstance(d, dict):
            raise PlanError("each task must be an object")
        try:
            for key in ("id", "title", "role", "goal"):
                if not isinstance(d[key], str):
                    raise PlanError(f"task.{key} must be a string")
            return cls(
                id=d["id"].strip(), title=d["title"].strip(), role=d["role"].strip(),
                goal=d["goal"].strip(),
                depends_on=_str_tuple(d.get("depends_on"), "depends_on"),
                owns=_str_tuple(d.get("owns"), "owns"),
                acceptance=_str_tuple(d.get("acceptance"), "acceptance"),
            )
        except KeyError as e:
            raise PlanError(f"task is missing field {e}") from e


@dataclass(frozen=True)
class Plan:
    version: int
    tasks: tuple[Task, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "tasks": [t.to_dict() for t in self.tasks]}

    @classmethod
    def from_dict(cls, d: Any) -> "Plan":
        if not isinstance(d, dict):
            raise PlanError("plan must be an object")
        tasks = d.get("tasks")
        if not isinstance(tasks, list):
            raise PlanError("plan.tasks must be a list")
        try:
            version = int(d.get("version", 1))
        except (TypeError, ValueError) as e:
            raise PlanError("plan.version must be an integer") from e
        return cls(version=version, tasks=tuple(Task.from_dict(t) for t in tasks))

    def by_id(self) -> dict[str, Task]:
        return {t.id: t for t in self.tasks}


# ---------------------------------------------------------------- graph helpers

def _ancestors(plan: Plan) -> dict[str, set[str]]:
    """task id -> all tasks it (transitively) depends on. Assumes deps are valid and acyclic."""
    tasks = plan.by_id()
    memo: dict[str, set[str]] = {}

    def visit(tid: str) -> set[str]:
        if tid in memo:
            return memo[tid]
        memo[tid] = set()  # guards against accidental recursion
        acc: set[str] = set()
        for dep in tasks[tid].depends_on:
            acc.add(dep)
            acc |= visit(dep)
        memo[tid] = acc
        return acc

    for tid in tasks:
        visit(tid)
    return memo


def _find_cycle_nodes(plan: Plan) -> list[str]:
    """Kahn's algorithm; returns ids left over (i.e. part of / behind a cycle)."""
    deps = {t.id: set(t.depends_on) for t in plan.tasks}
    ready = [t for t, d in deps.items() if not d]
    done: set[str] = set()
    while ready:
        n = ready.pop()
        done.add(n)
        for t, d in deps.items():
            if t not in done and t not in ready and d <= done:
                ready.append(t)
    return sorted(set(deps) - done)


def plan_stages(plan: Plan) -> list[list[str]]:
    """Group tasks into 'waves' by dependency depth, for display only.

    Tasks in the same wave are *candidates* for parallel execution. The real scheduler
    (Phase 5) works on readiness, not waves. Raises PlanError on a cyclic plan.
    """
    if _find_cycle_nodes(plan):
        raise PlanError("plan has a dependency cycle")
    tasks = plan.by_id()
    depth: dict[str, int] = {}

    def d(tid: str) -> int:
        if tid not in depth:
            deps = tasks[tid].depends_on
            depth[tid] = 0 if not deps else 1 + max(d(x) for x in deps)
        return depth[tid]

    waves: dict[int, list[str]] = {}
    for t in plan.tasks:
        waves.setdefault(d(t.id), []).append(t.id)
    return [waves[k] for k in sorted(waves)]


# ------------------------------------------------------------------ path overlap

def _parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p and p != "."]


def _has_glob(p: str) -> bool:
    return any(c in p for c in GLOB_CHARS)


def _glob_dir_prefix(p: str) -> list[str]:
    """Directory components before the first glob character: 'src/te*' -> ['src']."""
    idx = min(p.index(c) for c in GLOB_CHARS if c in p)
    before = p[:idx]
    dir_part = before[: before.rfind("/") + 1] if "/" in before else ""
    return _parts(dir_part)


def _is_ancestor_or_equal(a: list[str], b: list[str]) -> bool:
    return b[: len(a)] == a


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative: True if two owns-patterns might refer to a common file.

    Literal paths are treated as possibly being directories (so 'backend' overlaps
    'backend/app.py'). Two globs overlap if their directory prefixes are nested.
    False positives are acceptable (planner repairs them); false negatives are not.
    """
    pa, pb = a.replace("\\", "/").strip("/"), b.replace("\\", "/").strip("/")
    if pa == pb:
        return True
    ga, gb = _has_glob(pa), _has_glob(pb)
    if not ga and not gb:
        return _is_ancestor_or_equal(_parts(pa), _parts(pb)) or _is_ancestor_or_equal(_parts(pb), _parts(pa))
    if ga and gb:
        xa, xb = _glob_dir_prefix(pa), _glob_dir_prefix(pb)
        return _is_ancestor_or_equal(xa, xb) or _is_ancestor_or_equal(xb, xa)
    lit, glob = (pb, pa) if ga else (pa, pb)
    return fnmatch.fnmatch(lit, glob) or _is_ancestor_or_equal(_parts(lit), _glob_dir_prefix(glob))


def _owns_path_error(p: str) -> str | None:
    norm = p.replace("\\", "/")
    if not norm.strip():
        return "empty path"
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        return "must be relative to the project root"
    parts = _parts(norm)
    if ".." in parts:
        return "must not contain '..'"
    if parts and parts[0] == ".mission":
        return "must not point into .mission"
    return None


# --------------------------------------------------------------------- validation

def validate_plan(plan: Plan) -> list[str]:
    """Return a list of human/LLM-readable problems. Empty list means the plan is valid."""
    errors: list[str] = []
    if not plan.tasks:
        return ["plan has no tasks"]
    if len(plan.tasks) > MAX_TASKS:
        errors.append(f"plan has {len(plan.tasks)} tasks; the maximum is {MAX_TASKS}")

    seen: set[str] = set()
    for t in plan.tasks:
        if not ID_RE.match(t.id):
            errors.append(f"task id {t.id!r} is invalid (use lowercase letters, digits, '_' or '-', start with a letter)")
        if t.id in seen:
            errors.append(f"duplicate task id {t.id!r}")
        seen.add(t.id)
        for fld in ("title", "role", "goal"):
            if not getattr(t, fld):
                errors.append(f"task {t.id!r}: {fld} is empty")
        if not any(a for a in t.acceptance):
            errors.append(f"task {t.id!r}: needs at least one acceptance criterion")
        for p in t.owns:
            problem = _owns_path_error(p)
            if problem:
                errors.append(f"task {t.id!r}: owns {p!r}: {problem}")

    ids = {t.id for t in plan.tasks}
    deps_ok = True
    for t in plan.tasks:
        for dep in t.depends_on:
            if dep == t.id:
                errors.append(f"task {t.id!r} depends on itself")
                deps_ok = False
            elif dep not in ids:
                errors.append(f"task {t.id!r} depends on unknown task {dep!r}")
                deps_ok = False
    if not deps_ok or len(ids) != len(plan.tasks):
        return errors  # graph checks below need a sane graph

    cyc = _find_cycle_nodes(plan)
    if cyc:
        errors.append(f"dependency cycle involving: {', '.join(cyc)}")
        return errors

    anc = _ancestors(plan)
    tasks = list(plan.tasks)
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if a.id in anc[b.id] or b.id in anc[a.id]:
                continue  # ordered by dependencies -> can never run at the same time
            for pa in a.owns:
                for pb in b.owns:
                    if patterns_overlap(pa, pb):
                        errors.append(
                            f"tasks {a.id!r} and {b.id!r} may run in parallel but own overlapping paths "
                            f"({pa!r} vs {pb!r}); make one depend on the other or split the paths"
                        )
    return errors


def diff_plans(old: Plan, new: Plan) -> dict[str, list[str]]:
    o, n = old.by_id(), new.by_id()
    return {
        "added": [i for i in n if i not in o],
        "removed": [i for i in o if i not in n],
        "changed": [i for i in n if i in o and n[i] != o[i]],
    }


def plan_json_schema() -> dict[str, Any]:
    """JSON Schema handed to Ollama's `format` field so the model is constrained to a plan shape."""
    str_list = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "role": {"type": "string"},
                        "goal": {"type": "string"},
                        "depends_on": str_list,
                        "owns": str_list,
                        "acceptance": str_list,
                    },
                    "required": ["id", "title", "role", "goal", "depends_on", "owns", "acceptance"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }


# ------------------------------------------------------------- ownership at write time

def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Shell-like glob where '*' stays inside one path segment and '**' crosses directories."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        elif pattern[i] == "[" and "]" in pattern[i:]:
            j = pattern.index("]", i)
            out.append(pattern[i:j + 1]); i = j + 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def path_allowed(path: str, owns: Iterable[str]) -> bool:
    """True if `path` (relative to the project root) lies inside one of a task's `owns` patterns.

    This is the single-writer rule enforced IN CODE at write time, not just requested in the prompt.
    A literal owns entry ('backend', 'README.md') covers that file or that whole folder."""
    p = path.replace("\\", "/").strip("/")
    for pat in owns:
        q = pat.replace("\\", "/").strip("/")
        if not q:
            continue
        if _has_glob(q):
            if _glob_regex(q).match(p):
                return True
        elif p == q or p.startswith(q + "/"):
            return True
    return False


# --------------------------------------------------------------- replanning rules

FROZEN_STATUSES = frozenset({"running", "done", "verified", "failed"})


def validate_replan(old: Plan, new: Plan, statuses: Mapping[str, str], replaceable: Iterable[str] = ()) -> list[str]:
    """Rules for the lead agent editing the plan WHILE it runs.

    * the new plan must itself be a valid plan
    * a task that is running / done / verified / failed is history: it may not be removed or changed
    * pending (and skipped) tasks may be added, removed or edited freely
    """
    errors = validate_plan(new)
    new_by_id = new.by_id()
    swap = set(replaceable)          # a failed task the lead is replacing with smaller ones may go
    for t in old.tasks:
        st = statuses.get(t.id, "pending")
        if st not in FROZEN_STATUSES or (t.id in swap and t.id not in new_by_id):
            continue
        if t.id not in new_by_id:
            errors.append(f"task {t.id!r} is already {st} and cannot be removed")
        elif new_by_id[t.id] != t:
            errors.append(f"task {t.id!r} is already {st} and cannot be modified; return it exactly as it was")
    return errors


def replan_json_schema() -> dict[str, Any]:
    """Schema for the lead agent's review answer: keep the plan, or return the full updated task list."""
    tasks = plan_json_schema()["properties"]["tasks"]
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["keep", "update"]},
            "reason": {"type": "string"},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "tasks": tasks,
        },
        "required": ["action", "reason", "tasks"],
        "additionalProperties": False,
    }
