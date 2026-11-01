"""Local HTTP server (stdlib only).

    GET  /                 the UI            GET  /api/status   current session summary
    GET  /api/events       SSE stream        GET  /api/fs       folder browser for the picker
    POST /api/upload       one input file    POST /api/run      start a run
    POST /api/message      chat message      POST /api/command  pause/resume/stop
    POST /api/reset        forget the session (back to the start screen)
    GET  /api/models       models installed in Ollama, with sizes and the strength each would get
    POST /api/pick-folder  open the OS-native folder dialog on the user's desktop
    POST /api/approve      the go-ahead after planning (optionally another model per agent)
    POST /api/resolve      the user's decision on a task the lead could not rescue
    POST /api/retry        start a continuation run that redoes the failed tasks
    GET  /api/report       a Markdown diagnostic report of the current run (download)

Why SSE + POST instead of WebSocket: the event log is already an ordered, resumable feed.
SSE's `Last-Event-ID` IS our `after_seq`, so the browser's built-in reconnect resumes with no
gaps and no custom protocol. User actions are rare, so plain POSTs are enough.

Security: binds to 127.0.0.1 by default, rejects foreign Host headers (DNS rebinding) and
cross-origin browser requests (a random web page cannot drive your local agents).
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .engine import EngineConfig, EngineSession
from .events import Event
from .profiles import NAMES as STRENGTHS, detect
from .report import build_report, environment
from .llm import DEFAULT_HOST, DEFAULT_MODEL, LLMError, OllamaClient
from .paths import clean_path_input, looks_absolute
from .session import MockSession, SessionError
from .workspace import init_workspace

UI_DIR = Path(__file__).resolve().parent / "ui"
PICKER_SCRIPT = Path(__file__).resolve().parent / "folder_picker.py"
PICKER_TITLES = {"en": "Select the project folder", "fa": "پوشه‌ی پروژه را انتخاب کن"}
LANGS = ("en", "fa")
MAX_JSON_BYTES = 1_000_000
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
DRAIN_LIMIT = 4 * 1024 * 1024       # the most of an unread (refused) request body we read just to answer cleanly
HEARTBEAT_SECONDS = 15
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
BATCH_RE = re.compile(r"^[0-9a-f]{32}$")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}


def native_pick_folder(initial: str = "", title: str = PICKER_TITLES["en"], *, timeout: float = 900.0) -> dict[str, Any]:
    """Run folder_picker.py in a child process and parse its single JSON line."""
    try:
        r = subprocess.run([sys.executable, str(PICKER_SCRIPT), initial, title], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "the dialog was left open too long"}
    except OSError as e:
        return {"status": "error", "message": str(e)}
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        return {"status": "error", "message": (r.stderr or r.stdout or "no answer from the dialog").strip()[:300]}


def default_llm_factory(model: str, host: str | None):
    # No context limit is imposed here: Ollama (or the user's own Ollama setting) decides. A too-small
    # num_ctx was the root cause of the cut-off tool calls in the first live runs.
    return OllamaClient(model, host)


class App:
    """State shared by all request handlers. `llm_factory` and `pick_folder` are injectable (tests, other backends)."""

    def __init__(self, ui_dir: Path = UI_DIR, llm_factory=default_llm_factory, pick_folder=native_pick_folder) -> None:
        self.ui_dir = ui_dir
        self.llm_factory = llm_factory
        self.pick_folder = pick_folder
        self.picker_lock = threading.Lock()
        self.session: MockSession | EngineSession | None = None
        self.lock = threading.Lock()
        self.upload_root = Path(tempfile.mkdtemp(prefix="mission-uploads-"))
        atexit.register(shutil.rmtree, self.upload_root, ignore_errors=True)


def list_dir(path_str: str) -> dict[str, Any]:
    """Folder listing for the in-app fallback picker. Only directory *names* are exposed, never file contents."""
    path_str = clean_path_input(path_str)
    if path_str == "::drives" and hasattr(os, "listdrives"):  # Windows
        return {"path": "::drives", "parent": None, "dirs": [{"name": d, "path": d} for d in os.listdrives()],
                "writable": False, "sep": os.sep, "home": str(Path.home())}
    p = Path(path_str).expanduser() if path_str else Path.home()
    try:
        p = p.resolve()
        if not p.is_dir():
            raise SessionError(f"“{path_str}” is not a folder", 400, "not_a_folder", {"path": path_str})
        dirs = []
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            try:
                if child.is_dir() and not child.name.startswith("."):
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError as e:
        raise SessionError("No permission to open this folder", 403, "no_access", {"path": path_str}) from e
    except OSError as e:
        raise SessionError(f"Could not read the folder: {e}", 400, "fs_error", {"path": path_str}) from e
    parent: str | None = str(p.parent) if p.parent != p else ("::drives" if hasattr(os, "listdrives") else None)
    return {"path": str(p), "parent": parent, "dirs": dirs, "writable": os.access(p, os.W_OK),
            "sep": os.sep, "home": str(Path.home())}


class Handler(BaseHTTPRequestHandler):
    server_version = "MissionControl/0.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        pass

    # ------------------------------------------------------------------ helpers
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400, code: str | None = None, params: dict[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"error": message}
        if code:
            body["code"] = code
            body["params"] = params or {}
        self._send_json(body, status)

    def _fail(self, e: SessionError) -> None:
        self._error(str(e), e.status, e.code, e.params)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name.lower() in ALLOWED_HOSTS

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host")

    def _body_size(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _drain(self) -> None:
        """Read what is left of a request body we answered without reading. Closing a socket with unread data makes
        Windows reset the connection, and the client then gets an abort instead of our (error) answer."""
        left = self._unread
        if left > DRAIN_LIMIT:
            return              # not worth reading; this client may see a reset
        self._unread = 0
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            left -= len(chunk)

    def _read_json(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise SessionError("Invalid Content-Length header") from None
        if n > MAX_JSON_BYTES:
            raise SessionError("The request is too large", 413, "too_large")
        self._unread = 0
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise SessionError("The request body is not valid JSON") from None
        if not isinstance(data, dict):
            raise SessionError("The request body must be a JSON object")
        return data

    def _require_session(self):
        s = self.app.session
        if s is None:
            raise SessionError("There is no active run", 409, "no_session")
        return s

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error("Invalid Host header", 403, "bad_host")
        url = urlparse(self.path)
        try:
            if url.path == "/api/status":
                s = self.app.session
                return self._send_json({
                    "session": s is not None, "running": bool(s and s.is_running()),
                    "project_path": str(s.ws.root) if s else None, "mode": s.mode if s else None,
                })
            if url.path == "/api/models":
                return self._models(parse_qs(url.query).get("host", [""])[0].strip() or None)
            if url.path == "/api/report":
                return self._report()
            if url.path == "/api/fs":
                q = parse_qs(url.query)
                return self._send_json(list_dir(q.get("path", [""])[0]))
            if url.path == "/api/events":
                return self._sse(url)
            return self._static(url.path)
        except SessionError as e:
            self._fail(e)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else unquote(path.lstrip("/"))
        if "/" in name or "\\" in name or name.startswith("."):
            return self._error("Not found", 404, "not_found")
        f = self.app.ui_dir / name
        if not f.is_file():
            return self._error("Not found", 404, "not_found")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, url: Any) -> None:
        session = self._require_session()
        after = int(parse_qs(url.query).get("after", ["0"])[0] or 0)
        last_id = self.headers.get("Last-Event-ID")
        if last_id and last_id.isdigit():
            after = max(after, int(last_id))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(ev: Event) -> None:
            self.wfile.write(f"id: {ev.seq}\nevent: ev\ndata: {ev.to_json()}\n\n".encode("utf-8"))
            self.wfile.flush()

        q: queue.Queue[Event] = queue.Queue()
        unsubscribe = session.log.add_listener(q.put)  # subscribe BEFORE reading the backlog: no gap
        try:
            last = after
            for ev in session.log.read(after):
                emit(ev)
                last = ev.seq
            self.wfile.write(f"event: ready\ndata: {json.dumps({'last_seq': last})}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if ev.seq > last:  # events seen in the backlog are skipped
                    emit(ev)
                    last = ev.seq
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            unsubscribe()

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:
        self._unread = self._body_size()
        try:
            self._post()
        finally:
            self._drain()

    def _post(self) -> None:
        if not self._host_ok():
            return self._error("Invalid Host header", 403, "bad_host")
        if not self._origin_ok():
            return self._error("Requests from another origin are not allowed", 403, "bad_origin")
        url = urlparse(self.path)
        try:
            if url.path == "/api/upload":
                return self._upload(url)
            body = self._read_json()
            if url.path == "/api/pick-folder":
                return self._pick_folder(body)
            if url.path == "/api/approve":
                models = body.get("models") or {}
                if not isinstance(models, dict):
                    raise SessionError("models must be an object", 400)
                return self._send_json({"ok": True, **self._require_session().post_approval(models)})
            if url.path == "/api/resolve":
                s_ = self._require_session()
                return self._send_json({"ok": True, **s_.post_resolution(str(body.get("task_id", "")), str(body.get("decision", "")),
                                                                         str(body.get("note", "")))})
            if url.path == "/api/retry":
                return self._retry()
            if url.path == "/api/run":
                return self._run(body)
            if url.path == "/api/message":
                s = self._require_session()
                s.post_message(str(body.get("target", "")), str(body.get("text", "")))
                return self._send_json({"ok": True})
            if url.path == "/api/command":
                s = self._require_session()
                return self._send_json({"ok": True, **s.post_command(str(body.get("command", "")),
                                                                      str(body.get("target", "all")))})
            if url.path == "/api/reset":
                with self.app.lock:
                    if self.app.session:
                        self.app.session.stop()
                    self.app.session = None
                return self._send_json({"ok": True})
            return self._error("Not found", 404, "not_found")
        except SessionError as e:
            self._fail(e)

    def _upload(self, url: Any) -> None:
        batch = parse_qs(url.query).get("batch", [""])[0]
        if not BATCH_RE.match(batch):
            raise SessionError("Invalid upload batch id", 400, "bad_batch")
        name = Path(unquote(self.headers.get("X-Filename") or "")).name
        if not name or name in (".", ".."):
            raise SessionError("Invalid file name", 400, "bad_filename")
        try:
            size = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            size = -1
        if size < 0:
            raise SessionError("Content-Length is required", 411)
        if size > MAX_UPLOAD_BYTES:
            raise SessionError("The file is too large", 413, "too_large")
        folder = self.app.upload_root / batch
        folder.mkdir(exist_ok=True)
        dest = folder / name
        remaining = size
        with dest.open("wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        self._unread = 0
        self._send_json({"name": name, "size": size - remaining})

    def _models(self, host: str | None) -> None:
        llm = self.app.llm_factory(DEFAULT_MODEL, host)
        shown_host = getattr(llm, "host", host or DEFAULT_HOST)
        detailed = getattr(llm, "list_models_detailed", None)
        lister = getattr(llm, "list_models", None)
        try:
            if detailed:
                info = detailed()
            else:
                info = [{"name": n, "parameter_size": ""} for n in (lister() if lister else [])]
            details = [{"name": m["name"], "parameter_size": m.get("parameter_size", ""),
                        "strength": detect(m.get("parameter_size", ""), m["name"])} for m in info]
            self._send_json({"reachable": True, "models": [m["name"] for m in details], "details": details,
                             "default": DEFAULT_MODEL, "host": shown_host})
        except LLMError as e:
            self._send_json({"reachable": False, "models": [], "details": [], "default": DEFAULT_MODEL,
                             "host": shown_host, "error": str(e)})

    def _pick_folder(self, body: dict[str, Any]) -> None:
        initial = clean_path_input(str(body.get("initial", "")))
        lang = body.get("lang") if body.get("lang") in LANGS else "en"
        if not self.app.picker_lock.acquire(blocking=False):
            raise SessionError("A folder dialog is already open", 409, "picker_busy")
        try:
            result = self.app.pick_folder(initial, PICKER_TITLES[lang])
        finally:
            self.app.picker_lock.release()
        status = result.get("status")
        if status == "ok":
            return self._send_json({"path": result["path"]})
        if status == "cancel":
            return self._send_json({"cancelled": True})
        raise SessionError("The native folder dialog is not available on this machine", 501, "native_unavailable",
                           {"detail": str(result.get("message", ""))[:200]})

    def _retry(self) -> None:
        with self.app.lock:
            old = self.app.session
            if old is None or old.is_running() or not getattr(old, "can_continue", False):
                raise SessionError("There is nothing to retry: the run is still going, was a demo, or already succeeded",
                                   409, "cannot_retry")
            new = EngineSession.continuation(old)
            self.app.session = new
            new.start()
        self._send_json({"run_id": new.run_id, "project_path": str(new.ws.root), "mode": "live"})

    def _report(self) -> None:
        s = self._require_session()
        env = environment({"ollama host": getattr(getattr(s, "llm", None), "host", "—"),
                           "server": f"{self.server.server_address[0]}:{self.server.server_address[1]}"})
        body = build_report(s.log.read(), env).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="mission-report-{s.run_id}.md"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _preflight(llm: Any, model: str, host: str | None) -> None:
        """Fail BEFORE touching the project folder if Ollama is down or the model is missing."""
        lister = getattr(llm, "list_models", None)
        if lister is None:
            return
        try:
            installed = lister()
        except LLMError as e:
            raise SessionError(f"Cannot reach Ollama. Start it (`ollama serve`) or use Demo mode. ({e})", 502,
                               "ollama_unreachable", {"host": host or DEFAULT_HOST, "detail": str(e)[:200]}) from e
        wanted = {model, f"{model}:latest"}
        if not any(m in wanted for m in installed):
            raise SessionError(f"The model “{model}” is not installed in Ollama. Run: ollama pull {model}", 400,
                               "model_missing", {"model": model, "installed": installed[:20]})

    def _run(self, body: dict[str, Any]) -> None:
        prompt = str(body.get("prompt", "")).strip()
        project = clean_path_input(str(body.get("project_path", "")))
        if not prompt:
            raise SessionError("The request is empty; say what should be built", 400, "empty_prompt")
        if not project:
            raise SessionError("Choose the project folder", 400, "empty_path")
        if not looks_absolute(project):
            raise SessionError(f"“{project}” is not a full path. Use something like C:\\Projects\\site or /home/me/site",
                               400, "bad_path", {"path": project})
        path = Path(project)
        mode = body.get("mode", "demo")
        if mode not in ("demo", "live"):
            raise SessionError("mode must be “demo” or “live”", 400, "bad_mode")
        lang = body.get("lang") if body.get("lang") in LANGS else "en"
        try:
            speed = min(max(float(body.get("speed", 2.0)), 0.0), 100.0)
        except (TypeError, ValueError):
            raise SessionError("Invalid speed", 400, "bad_speed") from None
        batch = str(body.get("batch") or "")
        files: list[Path] = []
        if batch:
            if not BATCH_RE.match(batch):
                raise SessionError("Invalid upload batch id", 400, "bad_batch")
            folder = self.app.upload_root / batch
            files = sorted(p for p in folder.glob("*") if p.is_file()) if folder.is_dir() else []
        strength = str(body.get("strength") or "auto")
        if strength not in ("auto", *STRENGTHS):
            raise SessionError("strength must be auto, small, medium or large", 400, "bad_strength")
        try:
            num_ctx = int(body["num_ctx"]) if body.get("num_ctx") not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            raise SessionError("num_ctx must be a whole number", 400, "bad_ctx") from None
        if num_ctx is not None and not 2048 <= num_ctx <= 1_048_576:
            raise SessionError("num_ctx must be between 2048 and 1048576", 400, "bad_ctx")
        require_approval = bool(body.get("require_approval", False))
        on_failure = str(body.get("on_failure") or "ask")
        if on_failure not in ("ask", "skip", "stop"):
            raise SessionError("on_failure must be ask, skip or stop", 400, "bad_policy")
        llm, model, factory = None, "", None
        if mode == "live":
            model = str(body.get("model") or DEFAULT_MODEL).strip()
            host = str(body.get("ollama_host") or "").strip() or None

            def factory(m: str, _host=host, _ctx=num_ctx):
                client = self.app.llm_factory(m, _host)
                if _ctx and hasattr(client, "num_ctx"):
                    client.num_ctx = _ctx
                return client

            llm = factory(model)
            self._preflight(llm, model, host)
        with self.app.lock:
            if self.app.session and self.app.session.is_running():
                raise SessionError("A run is already in progress. End it first.", 409, "run_active")
            try:
                ws = init_workspace(path, files)
            except OSError as e:
                raise SessionError(f"The folder could not be created or written: {e}", 400, "folder_unwritable",
                                   {"path": str(path), "detail": str(e)[:200]}) from e
            if mode == "live":
                cfg = EngineConfig(review=bool(body.get("review", True)), require_approval=require_approval,
                                   on_failure=on_failure, strength=strength,
                                   context=f"num_ctx={num_ctx}" if num_ctx else "Ollama default (no limit set by Mission Control)")
                session = EngineSession(ws, prompt, llm, model=model, lang=lang, config=cfg, llm_factory=factory)
            else:
                session = MockSession(ws, prompt, speed=speed, lang=lang, require_approval=require_approval)
            self.app.session = session
            session.start()
        self._send_json({"run_id": session.run_id, "project_path": str(ws.root), "inputs": [f.name for f in files], "mode": mode})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], app: App) -> None:
        super().__init__(addr, Handler)
        self.app = app


def make_server(host: str = "127.0.0.1", port: int = 8765, ui_dir: Path = UI_DIR, *,
                llm_factory=default_llm_factory, pick_folder=native_pick_folder) -> Server:
    return Server((host, port), App(ui_dir, llm_factory, pick_folder))


def main() -> None:
    ap = argparse.ArgumentParser(description="Mission Control local server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        httpd = make_server(args.host, args.port)
    except OSError as e:
        raise SystemExit(f"cannot listen on {args.host}:{args.port} ({e}). Try --port 8766") from e
    url = f"http://{args.host}:{args.port}/"
    print(f"Mission Control is running at {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
