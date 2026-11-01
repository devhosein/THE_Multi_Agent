"""Project workspace on disk.

    <project>/
      inputs/                 files the user dropped in (copied, originals untouched)
      .mission/
        plan.json             current plan (latest version)
        events.jsonl          the event log
        decisions.md          shared decision ledger every agent reads
      contracts/              shared interface specs agents write (API schema etc.)
      ...                     the actual project files agents create

`resolve_inside` is the path jail: every file path coming from an agent or the UI must go
through it, so nothing can escape the project folder (../, absolute paths, symlinks) or
tamper with .mission/ directly.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .plan import Plan

MISSION_DIR = ".mission"
INPUTS_DIR = "inputs"


class PathEscapeError(ValueError):
    """A requested path points outside the project (or into a protected area)."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def mission_dir(self) -> Path:
        return self.root / MISSION_DIR

    @property
    def inputs_dir(self) -> Path:
        return self.root / INPUTS_DIR

    @property
    def plan_path(self) -> Path:
        return self.mission_dir / "plan.json"

    @property
    def events_path(self) -> Path:
        return self.mission_dir / "events.jsonl"

    @property
    def decisions_path(self) -> Path:
        return self.mission_dir / "decisions.md"


def resolve_inside(root: str | Path, relative: str, *, allow_mission: bool = False) -> Path:
    """Resolve `relative` against `root`, refusing anything that escapes it."""
    root_p = Path(root).resolve()
    rel = relative.replace("\\", "/")
    if not rel.strip():
        raise PathEscapeError("empty path")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise PathEscapeError(f"absolute paths are not allowed: {relative!r}")
    target = (root_p / rel).resolve()  # follows symlinks that already exist
    if not target.is_relative_to(root_p):
        raise PathEscapeError(f"path escapes the project folder: {relative!r}")
    rel_parts = target.relative_to(root_p).parts
    if rel_parts and rel_parts[0] == MISSION_DIR and not allow_mission:
        raise PathEscapeError(f"{MISSION_DIR}/ is managed by the engine: {relative!r}")
    return target


def init_workspace(project_path: str | Path, input_files: Iterable[str | Path] = ()) -> Workspace:
    root = Path(project_path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root)
    ws.mission_dir.mkdir(exist_ok=True)
    if not ws.decisions_path.exists():
        ws.decisions_path.write_text(
            "# Decision ledger\n\nShared decisions. Every agent reads this before working.\n\n",
            encoding="utf-8",
        )
    copied = list(input_files)
    if copied:
        ws.inputs_dir.mkdir(exist_ok=True)
    for src in copied:
        src_p = Path(src).expanduser()
        if not src_p.is_file():
            raise FileNotFoundError(f"input file not found: {src}")
        dest = ws.inputs_dir / src_p.name
        n = 1
        while dest.exists():  # never overwrite: report.pdf -> report_2.pdf
            n += 1
            dest = ws.inputs_dir / f"{src_p.stem}_{n}{src_p.suffix}"
        shutil.copy2(src_p, dest)
    return ws


def save_plan(ws: Workspace, plan: Plan) -> None:
    ws.plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_plan(ws: Workspace) -> Plan:
    return Plan.from_dict(json.loads(ws.plan_path.read_text(encoding="utf-8")))


def append_decision(ws: Workspace, decision_id: str, text: str, source: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with ws.decisions_path.open("a", encoding="utf-8") as f:
        f.write(f"- **{decision_id}** ({source}, {stamp}): {text}\n")
