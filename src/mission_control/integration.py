"""Integration check: does the project work AS A WHOLE? Run by the engine, never by a model.

Every task can pass its own checks while the pieces still do not fit. A real run (docs/FAILURES.md, cafe-35b) built
a backend that served only the API and a frontend that called it from another origin with no CORS: all four tasks
were "verified" and the menu page stayed empty. This module tries the project the way a browser would, using only
the standard library:

  * start the backend (a Python file under the project that runs an http.server) and wait until it listens
  * request every HTML page and every local file it references (scripts, stylesheets, images)
  * find the API calls in the pages' JavaScript and call them: a missing route, a server error, a relative URL while
    the backend does not serve the pages, or a call to another origin without CORS are all reported
  * element ids the JavaScript looks up (getElementById / querySelector('#…')) must exist on a page that loads it
  * the classes a page uses must be styled by the stylesheets it loads (an agent wrote `.form-group` in the CSS and
    `form-row` in the HTML: the reservation form came out unstyled, live run cafe-35b-v5)
  * a page served by the backend must not make the browser's automatic /favicon.ico request fail
  * every link between the pages must lead somewhere (benchmark pilot: the pages were served as /menu.html, the
    navigation linked to /menu, a visitor clicking "Menu" got a 404 while every check passed)
  * when Playwright is installed, `browser.py` then uses the pages in a real browser (JavaScript errors, data that
    never reaches the page, forms that do nothing); files changed meanwhile (stored test bookings) are restored

Each `Problem` names the files involved, so the engine can route it to the task that owns them. The check never
raises and always stops the server it started."""
from __future__ import annotations

import http.client
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from . import browser as browser_check

SKIP_DIRS = {".mission", "node_modules", "__pycache__", ".git", ".venv", "venv", "inputs", "tests", "test"}
START_TIMEOUT = 8.0
API_RE = re.compile(r"""(['"`])((?:https?://[^'"`\s/]+)?(?:\$\{\s*(\w+)\s*\})?(/api(?:/[^'"`\s?#$]*)?))""")
BASE_RE = re.compile(r"""\b(\w+)\s*[:=]\s*(['"`])(https?://[^'"`\s]+?)/?\2""")
ID_LOOKUP_RE = re.compile(r"""getElementById\(\s*(['"])([\w-]+)\1\s*\)|querySelector(?:All)?\(\s*(['"])#([\w-]+)\3\s*\)""")
ID_MADE_RE = re.compile(r"""\bid\s*=\s*\\?(['"])([\w-]+)\\?\1|\.id\s*=\s*(['"`])([\w-]+)\3|setAttribute\(\s*['"]id['"]\s*,\s*['"]([\w-]+)['"]""")
PORT_LINE_RE = re.compile(r"port", re.I)
NUM_RE = re.compile(r"(?<![\d.])([1-9]\d{3,4})(?![\d.])")


@dataclass
class Problem:
    kind: str                    # server_start | page_missing | asset_missing | api_missing | api_error |
                                 # relative_api | cross_origin | dom_id | style_missing | link_broken
                                 # | js_error | data_missing | form_failed | low_contrast (browser.py)
    message: str                 # what is wrong and how to fix it, for the agent and the lead
    files: tuple[str, ...] = ()  # project-relative files involved (used to find the responsible task)
    minor: bool = False          # cosmetic (unstyled classes, no icon): sent back once, never fails a task or a run

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "files": list(self.files), "minor": self.minor}


@dataclass
class Report:
    ran: bool = False            # False: nothing to check (no pages, or no server to start)
    served_by: str = ""          # "backend" | "files" (pages opened from disk / a separate static server)
    backend: str | None = None
    port: int | None = None
    problems: list[Problem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    pages: dict[str, str] = field(default_factory=dict)          # page file -> URL path on the backend
    page_files: dict[str, tuple[str, ...]] = field(default_factory=dict)   # page file -> it and its local scripts

    def as_text(self) -> str:
        """For a model: the summary and every problem with the files involved."""
        lines = [self.summary()] + [f"- [{p.kind}{', minor' if p.minor else ''}] {p.message} "
                                    f"(files: {', '.join(f for f in p.files if f)})"
                                    for p in self.problems]
        if self.port:   # benchmark: an agent read "on port 55603" and hard-coded it; nothing then ran on the real port
            lines.append("note: the check ran your server on a temporary port of its own (through the PORT environment "
                         "variable); keep your server's own port as the contract says.")
        return "\n".join(lines + [f"note: {n}" for n in self.notes])

    def summary(self) -> str:
        if not self.ran:
            return "integration check: nothing to run (" + ("; ".join(self.notes) or "no pages and no server") + ")"
        head = f"integration check: backend {self.backend or '-'}, pages served by {self.served_by or '-'}: "
        return head + (f"{len(self.problems)} problem(s)" if self.problems else "everything fits together")


# ------------------------------------------------------------------ project scan
def _files(root: Path, suffix: str) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*" + suffix)):
        rel = p.relative_to(root).parts
        if not (set(rel[:-1]) & SKIP_DIRS) and p.is_file():
            out.append(p)
    return out


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def find_backend(root: Path) -> Path | None:
    """The Python file that runs the project's HTTP server (a `__main__` entry using http.server)."""
    cands = []
    for p in _files(root, ".py"):
        if p.name.startswith("test_") or p.name == "conftest.py":
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "__main__" in src and ("http.server" in src or "HTTPServer" in src):
            rel = p.relative_to(root).parts
            cands.append(((0 if rel[0] in ("backend", "server", "api") else 1, 0 if p.stem in ("server", "app", "main") else 1,
                           len(rel)), p))
    return sorted(cands)[0][1] if cands else None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self.assets: list[str] = []
        self.inline: list[str] = []
        self.classes: set[str] = set()
        self.links: list[str] = []
        self.styles: list[str] = []      # <style> blocks
        self.icon = False                # <link rel="icon">: the browser then does not ask for /favicon.ico
        self._in_script = self._in_style = self._in_body = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        self.classes.update((a.get("class") or "").split())
        if tag == "body":
            self._in_body = True
        if tag == "link" and "icon" in (a.get("rel") or "").lower().split():
            self.icon = self.icon or not self._in_body    # browsers read icons in <head> only (benchmark run)
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "style":
            self._in_style = True
        if a.get("style"):
            self.assets += _css_urls(a["style"])
        if tag == "script":
            if a.get("src"):
                self.scripts.append(a["src"])
            else:
                self._in_script = True
        elif tag == "link" and a.get("href") and "stylesheet" in (a.get("rel") or "").lower():
            self.assets.append(a["href"])
        elif tag in ("img", "source", "audio", "video") and a.get("src"):
            self.assets.append(a["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_script:
            self.inline.append(data)
        elif self._in_style:
            self.styles.append(data)


def _local(ref: str) -> bool:
    return bool(ref) and not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", ref, re.I)


def _resolve(page_rel: str, ref: str) -> str:
    """'static/app.js' referenced from 'pages/menu.html' -> 'pages/static/app.js'; '/x.js' -> 'x.js'."""
    ref = ref.split("#")[0].split("?")[0]
    if ref.startswith("/"):
        parts = PurePosixPath(ref.lstrip("/")).parts
    else:
        parts = PurePosixPath(page_rel).parent.joinpath(ref).parts
    out: list[str] = []
    for part in parts:
        if part == "..":
            if out:
                out.pop()
        elif part not in (".", ""):
            out.append(part)
    return "/".join(out)


def _guarded_ids(code: str) -> set[str]:
    """Ids whose lookup result is checked before use (`const b = getElementById('x'); if (b) …`): a missing element
    is then a deliberate 'not on this page', not a crash."""
    out = set()
    for m in re.finditer(r"""(\w+)\s*=\s*document\.(?:getElementById\(\s*(['"])([\w-]+)\2|querySelector\(\s*(['"])#([\w-]+)\4)""", code):
        var, ident = m.group(1), m.group(3) or m.group(5)
        v = re.escape(var)
        if re.search(rf"if\s*\(\s*!?\s*{v}\s*(?:\)|&&|\|\|)|\b{v}\s*&&|\b{v}\?\.|{v}\s*(?:===|!==|==|!=)\s*null", code):
            out.add(ident)
    return out


def css_classes(text: str) -> set[str]:
    """The class names a stylesheet styles (selectors only: comments, strings, url() and declarations removed)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"url\([^)]*\)|(['\"]).*?\1", "", text)
    text = re.sub(r"\{[^{}]*\}", "{}", text)
    return set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", text))


def _string_words(code: str) -> set[str]:
    """Every word inside the string literals of a script: classes it adds, selectors it queries, HTML it builds."""
    out: set[str] = set()
    for m in re.finditer(r"(['\"`])((?:\\.|(?!\1).)*?)\1", code, re.S):
        out.update(re.findall(r"[\w-]+", m.group(2)))
    return out


def _norm(body: bytes) -> bytes:
    return body.replace(b"\r\n", b"\n").strip()


def _css_urls(text: str) -> list[str]:
    return [u.strip() for _, u in re.findall(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", text)]


@dataclass
class _ApiCall:
    path: str
    method: str
    origin: str | None           # "http://localhost:8000" for an absolute URL, None for a relative one
    file: str


def _method(src: str, at: int) -> str:
    """The HTTP method of the fetch call whose URL ends at `at`: look only at this call's own options."""
    window = src[at: at + 400]
    ends = [i for i in (window.find(");"), window.find("fetch("), window.find("\n\n")) if i >= 0]
    window = window[: min(ends)] if ends else window
    m = re.search(r"""method\s*:\s*['"](POST|PUT|PATCH|DELETE)['"]""", window, re.I)
    return m.group(1).upper() if m else "GET"


def _entries(data) -> int:
    """How many records (objects inside lists) a JSON value holds, at any depth."""
    if isinstance(data, list):
        return sum(1 if isinstance(v, dict) else 0 for v in data) + sum(_entries(v) for v in data if isinstance(v, dict))
    if isinstance(data, dict):
        return sum(_entries(v) for v in data.values())
    return 0


def _data_behind_empty_answer(root: Path, raw: bytes, path: str) -> tuple[str, int] | None:
    """An API that answers an empty collection while the data file for that resource is full (benchmark, cafe: the
    server read backend/data/menu.json, empty; the menu was in data/menu.json; the page showed no menu)."""
    import json
    try:
        answer = json.loads(raw)
    except ValueError:
        return None
    if _entries(answer) or not isinstance(answer, (dict, list)):
        return None
    word = path.rstrip("/").rsplit("/", 1)[-1].lower()         # /api/menu -> menu: only the file for this resource
    for p in _files(root, ".json"):
        if p.name in ("package.json", "package-lock.json", "tsconfig.json") or not word or word not in p.stem.lower():
            continue
        try:
            data = json.loads(p.read_bytes())
        except ValueError:
            continue
        if _entries(data):
            return _rel(root, p), _entries(data)
    return None


def _dynamic(src: str, end: int) -> bool:
    """The URL goes on with a value only known at run time: `/api/tasks/${id}` or "/api/tasks/" + id. Calling the
    fixed part alone tests a route the page never uses (benchmark, task board: PATCH /api/tasks -> 405, a fix task
    then chased a route that was fine)."""
    rest = src[end:end + 40]
    return rest.startswith("$") or bool(re.match(r"""['"`]\s*\+""", rest))


def _api_calls(src: str, file: str) -> list[_ApiCall]:
    bases = {m.group(1): m.group(3) for m in BASE_RE.finditer(src)}
    calls: list[_ApiCall] = []
    for m in API_RE.finditer(src):
        if _dynamic(src, m.end()):
            continue
        literal, var, path = m.group(2), m.group(3), m.group(4)
        origin = None
        if literal.startswith("http"):
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(literal))
        elif var and var in bases:
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(bases[var]))
        path = path.rstrip("/") or "/api"
        calls.append(_ApiCall(path, _method(src, m.end()), origin, file))
    for name, base in bases.items():                       # `${API}/menu` where API = "http://host:port/api"
        for m in re.finditer(r"\$\{\s*" + re.escape(name) + r"\s*\}(/[\w/-]*)", src):
            if _dynamic(src, m.end()):
                continue
            u = urlsplit(base)
            if "/api" in u.path or "/api" in m.group(1):
                calls.append(_ApiCall((u.path.rstrip("/") + m.group(1)).rstrip("/"), _method(src, m.end()),
                                      f"{u.scheme}://{u.netloc}", file))
    seen, out = set(), []
    for c in calls:
        key = (c.path, c.method, c.origin)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ------------------------------------------------------------------ HTTP helpers
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _request(port: int, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    """(status, headers{lowercase}, body) or (0, {}, error text)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read(200_000)
    except (OSError, http.client.HTTPException) as e:
        return 0, {}, str(e).encode()
    finally:
        conn.close()


def _source_ports(src: str) -> list[int]:
    ports = []
    for line in src.splitlines():
        if PORT_LINE_RE.search(line):
            ports += [int(n) for n in NUM_RE.findall(line) if 1024 <= int(n) <= 65535]
    return list(dict.fromkeys(ports))


def _start(backend: Path, root: Path, report: Report) -> tuple[subprocess.Popen | None, int | None]:
    src = backend.read_text(encoding="utf-8", errors="replace")
    defaults = _source_ports(src)
    port_env = _free_port()
    candidates = [port_env] + [p for p in defaults if not _listening(p)]
    busy = [p for p in defaults if _listening(p)]
    if busy and len(busy) == len(defaults):
        report.notes.append(f"default port(s) {busy} already in use on this machine; asked the server for {port_env} via PORT")
    env = {**os.environ, "PORT": str(port_env), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.Popen([sys.executable, str(backend)], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        report.problems.append(Problem("server_start", f"{_rel(root, backend)} could not be started: {e}", (_rel(root, backend),)))
        return None, None
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()
            report.problems.append(Problem(
                "server_start", f"{_rel(root, backend)} exited at start (code {proc.returncode}): {err[-600:] or 'no output'}",
                (_rel(root, backend),)))
            return None, None
        for p in candidates:
            if _listening(p):
                return proc, p
        time.sleep(0.15)
    report.problems.append(Problem(
        "server_start", f"{_rel(root, backend)} started but did not listen within {START_TIMEOUT:g}s (the check offered "
        f"it a free port through the PORT environment variable and also watched {defaults or 'the ports in the code'})",
        (_rel(root, backend),)))
    return proc, None


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
    if proc.stderr:
        proc.stderr.close()


# ------------------------------------------------------------------ the check
DATA_SUFFIXES = (".json", ".jsonl", ".csv", ".txt", ".db", ".sqlite", ".sqlite3")


def _data_files(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for suffix in DATA_SUFFIXES for p in _files(root, suffix)}


def check_integration(root: str | Path, browser: str | None = None) -> Report:
    """`browser`: auto | off | a channel (default: the MC_BROWSER_CHECK environment variable, else auto)."""
    root = Path(root).resolve()
    report = Report()
    started: dict[str, subprocess.Popen | None] = {}
    before = _data_files(root)                 # the check books tables and posts {}: undo what it stored
    try:
        _check(root, report, started)
        if report.port and report.pages:
            browser_check.check_pages(
                report.port, report.pages, lambda rel: report.page_files.get(rel, (rel,)), report.backend,
                lambda kind, msg, files, minor=False: report.problems.append(Problem(kind, msg, files, minor)),
                report.notes.append, browser)
    except Exception as e:  # noqa: BLE001 - the check must never take the engine down
        report.notes.append(f"the integration check itself failed: {type(e).__name__}: {e}")
    finally:
        _stop(started.get("proc"))
        try:
            for path, data in _data_files(root).items():
                if path not in before:
                    path.unlink()
                elif before[path] != data:
                    path.write_bytes(data)
        except OSError as e:
            report.notes.append(f"could not restore a data file: {e}")
    return report


def _check(root: Path, report: Report, started: dict) -> None:
    pages = _files(root, ".html")
    backend = find_backend(root)
    report.backend = _rel(root, backend) if backend else None
    if not pages and not backend:
        report.notes.append("no HTML pages and no Python http.server backend")
        return
    report.ran = True
    parsed: dict[str, _PageParser] = {}
    for p in pages:
        pp = _PageParser()
        try:
            pp.feed(p.read_bytes().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - a broken page is reported by the other checks
            pass
        parsed[_rel(root, p)] = pp

    port = None
    if backend:
        started["proc"], port = _start(backend, root, report)
        report.port = port

    # where the pages come from: the backend (same origin as its API) or the files themselves
    page_url: dict[str, str] = {}
    tried: list[str] = []                     # evidence for the fixer: what the server answered for the pages
    if port:
        for rel in parsed:
            parts = rel.split("/")              # a server may serve any folder as its root: try every suffix
            for cand in ["/" + "/".join(parts[i:]) for i in range(len(parts))]:
                st, hd, _ = _request(port, "GET", cand)
                if len(tried) < 6:
                    tried.append(f"GET {cand} -> {st or 'no answer'}")
                if st == 200 and "html" in hd.get("content-type", "html"):
                    page_url[rel] = cand
                    break
        # pages served under other names (/menu for frontend/menu.html): follow the pages' own links and compare
        # what comes back with the files (live run cafe-35b-v5)
        missing = {rel: _norm((root / rel).read_bytes()) for rel in parsed if rel not in page_url}
        if missing:
            hrefs = ["/"] + ["/" + h.split("#")[0].split("?")[0].lstrip("/") for pp in parsed.values() for h in pp.links
                             if _local(h) and h.startswith("/")]
            for href in list(dict.fromkeys(hrefs))[:25]:
                st, hd, body = _request(port, "GET", href)
                hit = next((rel for rel, want in missing.items() if st == 200 and _norm(body) == want), None)
                if hit:
                    page_url[hit] = href
                    del missing[hit]
        st, hd, _ = _request(port, "GET", "/")
        tried.insert(0, f"GET / -> {st or 'no answer'}")
        if st == 200 and "html" in hd.get("content-type", "") and not page_url:
            page_url = {rel: "/" + rel for rel in parsed}
        elif page_url and not (st == 200 and "html" in hd.get("content-type", "")):
            report.problems.append(Problem(                  # the address a visitor opens first
                "page_missing", f"the server serves the pages ({', '.join(sorted(page_url.values())[:4])}) but answers "
                f"{st or 'nothing'} for / , the address a visitor opens first: serve the landing page there",
                (report.backend or "",)))
    evidence = f" (the server answered: {', '.join(tried[:5])})" if tried and not page_url else ""
    report.served_by = "backend" if page_url else "files" if parsed else ""
    report.pages = dict(page_url)
    report.page_files = {rel: tuple(dict.fromkeys([rel] + [_resolve(rel, r) for r in pp.scripts if _local(r)]))
                         for rel, pp in parsed.items()}
    if port and parsed and page_url and len(page_url) < len(parsed):
        for rel in parsed:
            if rel not in page_url:
                report.problems.append(Problem(
                    "page_missing", f"the server serves some pages but not {rel} (GET /{rel} does not return it)",
                    (report.backend or "", rel)))

    # local files referenced by the pages
    for rel, pp in parsed.items():
        for ref in pp.scripts + pp.assets:
            if not _local(ref):
                continue
            target = _resolve(rel, ref)
            if rel in page_url and port:
                base = page_url[rel].rsplit("/", 1)[0] + "/"
                url = ref.split("#")[0].split("?")[0] if ref.startswith("/") else _resolve(base.lstrip("/") + "x", ref)
                url = "/" + url.lstrip("/")
                st, _, _ = _request(port, "GET", url)
                if st != 200:
                    report.problems.append(Problem(
                        "asset_missing", f"{rel} loads {ref}, but the server answers {st or 'nothing'} for {url}"
                        + ("" if (root / target).is_file() else " (the file does not exist)"), (rel, target)))
            elif not (root / target).is_file():
                report.problems.append(Problem("asset_missing", f"{rel} loads {ref}, but {target} does not exist", (rel,)))
    # links between the pages, followed like a visitor would (only through the backend: a file server is not ours)
    if port and page_url:
        answers: dict[str, int] = {}
        for rel, pp in parsed.items():
            if rel not in page_url:
                continue
            base = page_url[rel].rsplit("/", 1)[0] + "/"
            bad = []
            for href in dict.fromkeys(pp.links):
                target = href.split("#")[0].split("?")[0]
                if not target or not _local(href) or target.endswith((".pdf", ".zip")):
                    continue
                url = target if target.startswith("/") else "/" + _resolve(base.lstrip("/") + "x", target)
                if url not in answers:
                    answers[url] = _request(port, "GET", url)[0]
                if not (200 <= answers[url] < 400):
                    bad.append(f"{href} ({answers[url] or 'no answer'})")
            if bad:
                report.problems.append(Problem(
                    "link_broken", f"{rel} links to {', '.join(bad)}: a visitor who clicks gets an error. The server "
                    f"serves the pages at {', '.join(sorted(set(page_url.values())))}; make the links and the server's "
                    "routes agree.", tuple(f for f in (rel, report.backend) if f)))

    # files referenced from the stylesheets (url(...)), relative to each stylesheet; checked once per stylesheet
    seen_css: set[str] = set()
    for rel, pp in parsed.items():
        for ref in pp.assets:
            css = _resolve(rel, ref)
            if not (_local(ref) and css.endswith(".css") and css not in seen_css and (root / css).is_file()):
                continue
            seen_css.add(css)
            for u in _css_urls((root / css).read_bytes().decode("utf-8", "replace")):
                if _local(u) and not (root / _resolve(css, u)).is_file():
                    report.problems.append(Problem(
                        "asset_missing", f"{css} uses url({u}), but {_resolve(css, u)} does not exist", (css,)))

    # JavaScript: API calls and element ids
    js_src: dict[str, str] = {}
    for rel, pp in parsed.items():
        for ref in pp.scripts:
            if _local(ref):
                t = _resolve(rel, ref)
                if (root / t).is_file() and t not in js_src:
                    js_src[t] = (root / t).read_bytes().decode("utf-8", "replace")
        for i, code in enumerate(pp.inline):
            js_src[f"{rel}#inline{i}"] = code
    made_ids = set()
    for code in list(js_src.values()) + [p.read_bytes().decode("utf-8", "replace") for p in pages]:
        for m in ID_MADE_RE.finditer(code):
            made_ids.add(m.group(2) or m.group(4) or m.group(5))
    for js, code in js_src.items():
        loaders = [rel for rel, pp in parsed.items() if js.startswith(rel + "#") or
                   any(_local(r) and _resolve(rel, r) == js for r in pp.scripts)]
        wanted = {m.group(2) or m.group(4) for m in ID_LOOKUP_RE.finditer(code)} - _guarded_ids(code)
        missing = sorted(i for i in wanted if i not in made_ids and not any(i in parsed[r].ids for r in loaders))
        if missing and loaders:
            owner = js.split("#")[0]
            report.problems.append(Problem(
                "dom_id", f"{owner} looks up element id(s) {', '.join('#' + i for i in missing)}, but no page that loads "
                f"it ({', '.join(loaders)}) has such an element: the lookup returns null and the script breaks there",
                tuple(dict.fromkeys([owner] + loaders))))

    # classes each page uses that none of its stylesheets styles (scripts may use classes as hooks: those are fine)
    css_files = _files(root, ".css")

    def sheet(rel: str, ref: str) -> tuple[str, str | None]:
        """(the stylesheet's file, its text) as the browser gets it: through the server when it serves the page."""
        if rel in page_url and port:
            base = page_url[rel].rsplit("/", 1)[0] + "/"
            url = ref.split("#")[0].split("?")[0] if ref.startswith("/") else _resolve(base.lstrip("/") + "x", ref)
            url = "/" + url.lstrip("/")
            st, _, body = _request(port, "GET", url)
            if st != 200:
                return url, None
            same = next((_rel(root, c) for c in css_files if _norm(c.read_bytes()) == _norm(body)), url.lstrip("/"))
            return same, body.decode("utf-8", "replace")
        target = _resolve(rel, ref)
        return target, (root / target).read_bytes().decode("utf-8", "replace") if (root / target).is_file() else None

    script_words = set().union(*(_string_words(c) for c in js_src.values())) if js_src else set()
    css_texts: dict[str, str] = {}                          # every stylesheet the pages use, as the browser gets it
    for rel, pp in parsed.items():
        refs = [ref for ref in pp.assets if ref.split("?")[0].endswith(".css")]
        if not (refs or pp.styles) or any(not _local(ref) for ref in refs):
            continue                                        # no own stylesheet, or a CDN one we cannot read
        got = [sheet(rel, ref) for ref in refs]
        if any(text is None for _, text in got):
            continue                                        # a missing stylesheet is reported as asset_missing
        sheets = [f for f, _ in got]
        css_texts.update({f: t or "" for f, t in got})
        if pp.styles:
            css_texts[rel] = " ".join(pp.styles)
        styled = css_classes(" ".join(pp.styles + [text or "" for _, text in got]))
        unstyled = sorted(pp.classes - styled - script_words)
        if len(unstyled) >= 2:
            used = set().union(*(q.classes for q in parsed.values())) | script_words
            spare = sorted(styled - used)
            report.problems.append(Problem(
                "style_missing", f"{rel} uses class(es) {', '.join(unstyled[:20])} that no stylesheet it loads "
                f"({', '.join(sheets) or 'its <style>'}) defines, so those parts show up unstyled"
                + (f"; the stylesheet styles {', '.join(spare[:20])}, which no page uses (a name mismatch?)" if spare else "")
                + ". Use the same class names in the HTML and the CSS.", tuple(dict.fromkeys([rel] + sheets)), minor=True))

    # CSS variables used without a fallback but defined nowhere: the browser drops the whole declaration (live run
    # cafe-35b-v8: `background: …, var(--coffee-800)` with only 900/700/500/300 defined -> white headings on cream)
    defined = set(re.findall(r"(--[\w-]+)\s*:", " ".join(css_texts.values())))
    defined |= {m for c in js_src.values() for m in re.findall(r"""setProperty\(\s*['"](--[\w-]+)""", c)}
    for css, text in css_texts.items():
        missing = sorted(set(re.findall(r"var\(\s*(--[\w-]+)\s*\)", text)) - defined)
        if missing:
            near = sorted({d for d in defined for m in missing if d.rsplit("-", 1)[0] == m.rsplit("-", 1)[0]})
            report.problems.append(Problem(
                "style_missing", f"{css} uses the CSS variable(s) {', '.join(missing[:10])}, defined nowhere: the browser "
                "drops every declaration that uses them (colours, backgrounds), so those parts lose their look"
                + (f"; defined are {', '.join(near[:10])}" if near else "") + ". Define them or use existing ones.",
                (css,), minor=True))

    if not port:
        return
    if page_url and not any(pp.icon for pp in parsed.values()):
        st, _, _ = _request(port, "GET", "/favicon.ico")
        if st != 200:
            report.problems.append(Problem(
                "asset_missing", f"browsers ask every site for /favicon.ico; the server answers {st or 'nothing'} and no "
                "page declares an icon, so every page shows a failed request in the browser console. Add "
                "<link rel=\"icon\" href=\"...\"> to each page's <head> (an inline SVG data: URL is enough) or let the "
                "server answer /favicon.ico.", tuple(parsed), minor=True))
    page_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} if page_url else set()
    default_ports = _source_ports(backend.read_text(encoding="utf-8", errors="replace")) if backend else []
    done: set[tuple[str, str]] = set()
    for js, code in js_src.items():
        owner = js.split("#")[0]
        for c in _api_calls(code, owner):
            if (c.path, c.method) in done and c.origin is None:
                continue
            done.add((c.path, c.method))
            files = (report.backend or "", owner)
            body = b"{}" if c.method == "POST" else None
            st, hd, raw = _request(port, c.method, c.path, body, {"Content-Type": "application/json"} if body else {})
            if st in (0, 404, 405, 501):
                report.problems.append(Problem(
                    "api_missing", f"{owner} calls {c.method} {c.path}, but the server answers {st or 'nothing'} "
                    "(no such route?)", files))
                continue
            if st >= 500:
                report.problems.append(Problem(
                    "api_error", f"{c.method} {c.path} (called by {owner}) fails with HTTP {st}: "
                    f"{raw[:300].decode('utf-8', 'replace')}", files))
            if c.method == "GET" and st == 200:
                held = _data_behind_empty_answer(root, raw, c.path)
                if held:
                    report.problems.append(Problem(
                        "data_missing", f"GET {c.path} answers {raw[:120].decode('utf-8', 'replace')!r}, nothing in it, "
                        f"but {held[0]} holds {held[1]} entries: the server reads another file, or "
                        "loses them on the way", (report.backend or "",)))
            if c.origin is None:
                if not page_url:
                    report.problems.append(Problem(
                        "relative_api", f"{owner} calls the relative URL {c.path}, but the backend does not serve the "
                        f"pages{evidence}, so in a browser the request goes to whatever serves the HTML, not to the API. "
                        "If the server has code to serve the pages, it does not work: check how it maps a URL path like "
                        "/index.html to a file (a leading '/' makes os.path.join drop the folder). Fix: "
                        "let the backend serve the pages too (same origin, recommended), or call the API's full address "
                        "and add CORS.", files))
                continue
            u = urlsplit(c.origin)
            if u.port and u.port != port and u.port not in default_ports:
                report.problems.append(Problem(
                    "api_missing", f"{owner} calls {c.origin}{c.path}, but the server listens on "
                    f"{default_ports or [port]}", files))
            if c.origin.rstrip("/") in page_origins:
                continue
            origin = "http://127.0.0.1:5500"         # a page opened from anywhere else is another origin
            if page_url:
                origin = f"http://127.0.0.1:{port}"  # same server, but host/port spelled differently = another origin
            st, hd, _ = _request(port, "GET" if c.method == "GET" else "OPTIONS", c.path, None,
                                 {"Origin": origin} if c.method == "GET" else
                                 {"Origin": origin, "Access-Control-Request-Method": c.method,
                                  "Access-Control-Request-Headers": "content-type"})
            allow = hd.get("access-control-allow-origin", "")
            if allow not in ("*", origin) or (c.method != "GET" and not (200 <= st < 300)):
                where = "the pages are served by the backend on another host/port spelling" if page_url else \
                    f"the pages are not served by the backend{evidence}"
                report.problems.append(Problem(
                    "cross_origin", f"{owner} calls {c.origin}{c.path} ({c.method}) from another origin ({where}), "
                    f"and the server does not allow it (CORS{' preflight' if c.method != 'GET' else ''}: "
                    f"Access-Control-Allow-Origin={allow or 'missing'}, status {st}). A browser blocks this request. "
                    "Fix: serve the pages from the backend and call the API with relative URLs like /api/menu "
                    "(recommended), or add CORS headers and answer OPTIONS.", files))
