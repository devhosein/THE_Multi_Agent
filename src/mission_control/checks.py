"""Mechanical checks run after a worker says it is done.

This is deliberately narrow and honest: it verifies things a machine can verify without trusting the agent
(the files exist, are not empty, and parse). It does NOT judge whether acceptance criteria are met - that
is test-based / reviewer-based verification, planned for a later phase. Passing means `verified` in the UI
in exactly this limited sense."""
from __future__ import annotations

import ast
import json
from typing import Sequence

from .plan import Task
from .workspace import Workspace


def check_task(ws: Workspace, task: Task, written: Sequence[str]) -> list[str]:
    problems: list[str] = []
    if task.owns and not written:
        problems.append("No file was written, but this task owns files. Use write_file to create them.")
    for rel in written:
        path = ws.root / rel
        if not path.is_file():
            problems.append(f"{rel}: the file is missing from disk")
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if not text.strip():
            problems.append(f"{rel}: the file is empty")
            continue
        suffix = path.suffix.lower()
        if suffix == ".py":
            try:
                ast.parse(text)
            except SyntaxError as e:
                problems.append(f"{rel}: Python syntax error on line {e.lineno}: {e.msg}")
        elif suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as e:
                problems.append(f"{rel}: invalid JSON ({e.msg} at line {e.lineno})")
        elif suffix in (".html", ".htm"):
            if "<" not in text or ">" not in text:
                problems.append(f"{rel}: does not look like HTML")
        elif suffix == ".css":
            if text.count("{") != text.count("}"):
                problems.append(f"{rel}: unbalanced braces in CSS")
    return problems
