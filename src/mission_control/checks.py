"""Mechanical checks run after a worker says it is done.

This is deliberately narrow and honest: it verifies things a machine can verify without trusting the agent
(the files exist, are not empty, and parse). It does NOT judge whether acceptance criteria are met - that
is test-based / reviewer-based verification, planned for a later phase. Passing means `verified` in the UI
in exactly this limited sense."""
from __future__ import annotations

import ast
import json
import re
from typing import Sequence

from .plan import Task
from .workspace import Workspace

# A contract must say how the parts run together (real run cafe-35b: the contract described every endpoint but not who
# serves the pages, so the frontend and the backend each assumed something different).
INTEGRATION_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(?:\d+[.)]\s*)?integration\b", re.I | re.M)


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
        if rel.startswith("contracts/") and suffix in (".md", ".txt") and not INTEGRATION_HEADING.search(text):
            problems.append(f"{rel}: the contract has no \"## Integration\" section. Add one that settles how the parts "
                            "run together: which program serves the pages, the port, the exact URLs the frontend calls "
                            "(recommended: the backend serves the pages and the frontend calls relative URLs like "
                            "/api/menu), and the start command.")
    return problems
