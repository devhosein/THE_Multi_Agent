"""The tools a worker agent can call.

Every rule that matters for safety and for "no write conflicts" is enforced HERE, in code, at the moment
of the call - never merely requested in the prompt, because a small model will sometimes ignore a prompt:

  * paths go through the project jail (`resolve_inside`): no `../`, no absolute paths, no `.mission/`
  * a task may write only inside its `owns` patterns (`path_allowed`)
  * every failure is returned to the model as an `ERROR: ...` string so it can correct itself
  * the short note that replaces written content in the history (worker.compact) is never written INTO a file: models
    imitate their own history, and live runs v4 and v7 overwrote server.py / appended to style.css with that note
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Iterable

from .eventlog import EventLog
from .events import T
from .plan import path_allowed
from .workspace import MISSION_DIR, PathEscapeError, Workspace, resolve_inside

MAX_FILE_BYTES = 256 * 1024
MAX_READ_BYTES = 30_000
MAX_LIST_ENTRIES = 300
CHUNK_CHARS = 48
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv"}
# what worker.compact says about a write it removed from the history (in the tool result), and older notes of that kind
REMOVED_NOTE = "characters of content were removed from this conversation to save space; read_file shows the file."
PLACEHOLDER_RE = re.compile(r"<<\d+ characters written to [^<>]*>>|\d+ " + re.escape(REMOVED_NOTE[:60]))
PLACEHOLDER_ERROR = ("ERROR: this is not file content: it is the short note that replaced one of your earlier writes in "
                     "the conversation (to save space). Nothing was written. Write the real content; read_file shows "
                     "what the file contains now.")

TOOL_SPECS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List the files and folders in the project (or in a sub-folder).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "folder relative to the project root; default '.'"}}}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the project (for example a contract under contracts/ or a file in inputs/). "
                       "Long files come back in pages: pass start_line to read further.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                        "start_line": {"type": "integer", "description": "first line to return, default 1"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create a file or replace its whole content. Only paths you own are allowed. For long files, write the "
                       "first part here and add the rest with append_file, so no single call gets too long.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "append_file",
        "description": "Add text to the END of a file you own (creates it if missing). Use it to write a long file in several parts.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "note_to_lead",
        "description": "Report to the lead agent AT ONCE when you find a problem you cannot fix in your own files: a "
                       "change needed in a file another agent owns, a contract that is wrong or unclear, a missing piece. "
                       "The lead answers within a step or two and can change the plan.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "check_project",
        "description": "Ask the engine to run the project the way a browser would: start the server, load every page "
                       "with its scripts, styles and images, call the API the pages call. Returns the problems found. "
                       "Use it after changing a server or pages, and before finish. You cannot run anything else.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Call this when every acceptance criterion is met. Give a short summary of what you built.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]


class ToolBox:
    """One instance per task; remembers what the task wrote so the checks and the lead can use it."""

    def __init__(self, ws: Workspace, owns: Iterable[str], log: EventLog, agent_id: str,
                 *, chunk_delay_cap: float = 0.02, emit=None, read_chars: int = MAX_READ_BYTES, on_note=None,
                 run_check=None) -> None:
        self.ws = ws
        self.owns = tuple(owns)
        self.log = log
        self.agent_id = agent_id
        self.chunk_delay_cap = chunk_delay_cap
        self._emit = emit or (lambda type_, data: log.append(type_, data, agent_id=agent_id))
        self.read_chars = read_chars
        self.on_note = on_note           # the engine's live channel to the lead (None: notes wait for the review)
        self.run_check = run_check       # the engine's integration check (None: not available here)
        self.written: list[str] = []
        self.notes: list[str] = []
        self.finished: str | None = None

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, args: Any) -> str:
        """Run a tool. Never raises: problems come back as 'ERROR: ...' text for the model."""
        if not isinstance(args, dict):
            return "ERROR: tool arguments must be a JSON object"
        fn = {"list_files": self._list_files, "read_file": self._read_file, "write_file": self._write_file,
              "append_file": self._append_file, "note_to_lead": self._note, "check_project": self._check_project,
              "finish": self._finish}.get(name)
        if fn is None:
            return (f"ERROR: unknown tool {name!r}. Available: list_files, read_file, write_file, append_file, "
                    "note_to_lead, check_project, finish")
        try:
            return fn(args)
        except OSError as e:
            return f"ERROR: {e.strerror or e}"

    # --------------------------------------------------------------------- tools
    def _rel(self, target) -> str:
        return target.relative_to(self.ws.root.resolve()).as_posix()

    def _list_files(self, args: dict[str, Any]) -> str:
        raw = args.get("path") or "."
        if not isinstance(raw, str):
            return "ERROR: path must be a string"
        try:
            base = self.ws.root.resolve() if raw.strip() in (".", "./") else resolve_inside(self.ws.root, raw)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        if not base.is_dir():
            return f"ERROR: {raw!r} is not a folder"
        root = self.ws.root.resolve()
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and d != MISSION_DIR)
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            for d in dirnames:
                out.append((d if rel_dir == "." else f"{rel_dir}/{d}") + "/")
            for f in sorted(filenames):
                if f.startswith(".") or f.endswith(".mc-tmp"):
                    continue
                out.append(f if rel_dir == "." else f"{rel_dir}/{f}")
            if len(out) >= MAX_LIST_ENTRIES:
                out.append("...[list truncated]")
                break
        return "\n".join(out) if out else "(empty folder)"

    def _read_file(self, args: dict[str, Any]) -> str:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return "ERROR: path is required"
        try:
            target = resolve_inside(self.ws.root, raw, allow_mission=True)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        rel = self._rel(target)
        if rel.startswith(MISSION_DIR) and rel != f"{MISSION_DIR}/decisions.md":
            return "ERROR: only .mission/decisions.md can be read from the engine folder"
        if not target.is_file():
            return f"ERROR: {rel} does not exist"
        data = target.read_bytes()
        if b"\x00" in data[:1024]:
            return f"ERROR: {rel} is a binary file"
        lines = data.decode("utf-8", errors="replace").splitlines(keepends=True)
        try:
            start = max(1, int(args.get("start_line") or 1))
        except (TypeError, ValueError):
            start = 1
        if start > max(1, len(lines)):
            return f"ERROR: {rel} has only {len(lines)} lines"
        out, size, end = [], 0, start - 1
        for line in lines[start - 1:]:
            if size + len(line) > self.read_chars and out:
                break
            if len(line) > self.read_chars:      # one enormous line (minified code): never return it whole
                line = line[: self.read_chars] + f"…[line {end + 1} is {len(line)} characters long; cut here]\n"
            out.append(line)
            size += len(line)
            end += 1
        text = "".join(out)
        if end < len(lines):   # a page, not the whole file: say exactly how to continue
            text += f"\n...[lines {start}-{end} of {len(lines)}; call read_file with start_line={end + 1} to read more]"
        return text

    def _write_file(self, args: dict[str, Any]) -> str:
        raw, content = args.get("path"), args.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return "ERROR: path is required"
        if not isinstance(content, str):
            return ("ERROR: content is missing: write_file needs the COMPLETE file content. (Earlier calls in this "
                    "conversation show no content only because it was removed to save space.)")
        if PLACEHOLDER_RE.search(content):
            return PLACEHOLDER_ERROR
        try:
            target = resolve_inside(self.ws.root, raw)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        rel = self._rel(target)
        if not path_allowed(rel, self.owns):
            allowed = ", ".join(self.owns) if self.owns else "nothing (this is a read-only task)"
            return f"ERROR: you may not write {rel!r}. You may only write inside: {allowed}"
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            return f"ERROR: file is too large ({len(data)} bytes; the limit is {MAX_FILE_BYTES})"
        if target.is_dir():
            return f"ERROR: {rel} is a folder"
        existed = target.exists()
        if existed and target.read_bytes() == data:
            # the loop seen in a real run: the same file written again and again. Say so plainly.
            if rel not in self.written:
                self.written.append(rel)
            return (f"OK: no change - {rel} already has exactly this content. Do not write it again; "
                    "continue with the next file, or call finish if your work is complete.")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".mc-tmp")
        tmp.write_bytes(data)          # bytes, so Windows does not turn \n into \r\n
        os.replace(tmp, target)        # atomic: a reader never sees half a file
        if rel not in self.written:
            self.written.append(rel)
        self._stream_events(rel, content, existed)
        return f"OK: wrote {rel} ({len(data)} bytes)"

    def _append_file(self, args: dict[str, Any]) -> str:
        raw, content = args.get("path"), args.get("content")
        if not isinstance(raw, str) or not raw.strip():
            return "ERROR: path is required"
        if not isinstance(content, str) or not content:
            return ("ERROR: content is missing: append_file needs the text to add. (Earlier calls in this "
                    "conversation show no content only because it was removed to save space.)")
        if PLACEHOLDER_RE.search(content):
            return PLACEHOLDER_ERROR
        try:
            target = resolve_inside(self.ws.root, raw)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        rel = self._rel(target)
        if not path_allowed(rel, self.owns):
            allowed = ", ".join(self.owns) if self.owns else "nothing (this is a read-only task)"
            return f"ERROR: you may not write {rel!r}. You may only write inside: {allowed}"
        if target.is_dir():
            return f"ERROR: {rel} is a folder"
        old = target.read_bytes() if target.exists() else b""
        data = old + content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            return f"ERROR: file would be too large ({len(data)} bytes; the limit is {MAX_FILE_BYTES})"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".mc-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        if rel not in self.written:
            self.written.append(rel)
        # the UI keeps the existing text and types the new part after it
        self._emit(T.FILE_CREATED, {"path": rel})
        chunks = [content[i:i + CHUNK_CHARS] for i in range(0, len(content), CHUNK_CHARS)]
        delay = min(self.chunk_delay_cap, 1.0 / max(1, len(chunks))) if self.chunk_delay_cap > 0 else 0
        for c in chunks:
            self._emit(T.FILE_CHUNK, {"path": rel, "text": c})
            if delay:
                time.sleep(delay)
        self._emit(T.FILE_SAVED, {"path": rel, "size": len(data)})
        return f"OK: appended {len(content.encode('utf-8'))} bytes to {rel} (now {len(data)} bytes)"

    def _stream_events(self, rel: str, content: str, rewrite: bool) -> None:
        """The file is already safely on disk; these events are what the UI shows. The chunks are paced so the
        file visibly 'types itself' (bounded to about a second per file)."""
        self._emit(T.FILE_CREATED, {"path": rel, **({"rewrite": True} if rewrite else {})})
        chunks = [content[i:i + CHUNK_CHARS] for i in range(0, len(content), CHUNK_CHARS)]
        delay = min(self.chunk_delay_cap, 1.0 / max(1, len(chunks))) if self.chunk_delay_cap > 0 else 0
        for c in chunks:
            self._emit(T.FILE_CHUNK, {"path": rel, "text": c})
            if delay:
                time.sleep(delay)
        self._emit(T.FILE_SAVED, {"path": rel, "size": len(content.encode("utf-8"))})

    def _note(self, args: dict[str, Any]) -> str:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return "ERROR: text is required"
        text = text.strip()[:1000]
        if self.on_note is not None:
            self.on_note(text)
            return "OK: the lead agent has your report and will answer shortly. Continue with your own files meanwhile."
        self.notes.append(text)
        return "OK: the lead agent will read your note after this task"

    def _check_project(self, args: dict[str, Any]) -> str:
        if self.run_check is None:
            return "ERROR: the project check is not available here"
        return "Result of running the project:\n" + self.run_check()

    def _finish(self, args: dict[str, Any]) -> str:
        summary = args.get("summary")
        self.finished = summary.strip() if isinstance(summary, str) and summary.strip() else "(no summary given)"
        return "OK: finished"
