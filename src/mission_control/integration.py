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
                                 # relative_api | cross_origin | dom_id
    message: str                 # what is wrong and how to fix it, for the agent and the lead
    files: tuple[str, ...] = ()  # project-relative files involved (used to find the responsible task)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message, "files": list(self.files)}


@dataclass
class Report:
    ran: bool = False            # False: nothing to check (no pages, or no server to start)
    served_by: str = ""          # "backend" | "files" (pages opened from disk / a separate static server)
    backend: str | None = None
    port: int | None = None
    problems: list[Problem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """For a model: the summary and every problem with the files involved."""
        lines = [self.summary()] + [f"- [{p.kind}] {p.message} (files: {', '.join(f for f in p.files if f)})"
                                    for p in self.problems]
        return "\n".join(lines + [f"note: {n}" for n in self.notes])

    def summary(self) -> str:
        if not self.ran:
            return "integration check: nothing to run (" + ("; ".join(self.notes) or "no pages and no server") + ")"
        head = f"integration check: backend {self.backend or '-'}" + (f" on port {self.port}" if self.port else "") + \
               f", pages served by {self.served_by or '-'}: "
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
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
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

    def handle_data(self, data):
        if self._in_script:
            self.inline.append(data)


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


def _api_calls(src: str, file: str) -> list[_ApiCall]:
    bases = {m.group(1): m.group(3) for m in BASE_RE.finditer(src)}
    calls: list[_ApiCall] = []
    for m in API_RE.finditer(src):
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
        "server_start", f"{_rel(root, backend)} started but did not listen on port {port_env} (PORT) "
        f"or on {defaults or 'any port written in the code'} within {START_TIMEOUT:g}s",
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
def check_integration(root: str | Path) -> Report:
    root = Path(root).resolve()
    report = Report()
    started: dict[str, subprocess.Popen | None] = {}
    try:
        _check(root, report, started)
    except Exception as e:  # noqa: BLE001 - the check must never take the engine down
        report.notes.append(f"the integration check itself failed: {type(e).__name__}: {e}")
    finally:
        _stop(started.get("proc"))
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
        st, hd, _ = _request(port, "GET", "/")
        tried.insert(0, f"GET / -> {st or 'no answer'}")
        if st == 200 and "html" in hd.get("content-type", "") and not page_url:
            page_url = {rel: "/" + rel for rel in parsed}
    evidence = f" (the server answered: {', '.join(tried[:5])})" if tried and not page_url else ""
    report.served_by = "backend" if page_url else "files" if parsed else ""
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
                url = ref if ref.startswith("/") else _resolve(base.lstrip("/") + "x", ref)
                url = "/" + url.lstrip("/")
                st, _, _ = _request(port, "GET", url)
                if st != 200:
                    report.problems.append(Problem(
                        "asset_missing", f"{rel} loads {ref}, but the server answers {st or 'nothing'} for {url}"
                        + ("" if (root / target).is_file() else " (the file does not exist)"), (rel, target)))
            elif not (root / target).is_file():
                report.problems.append(Problem("asset_missing", f"{rel} loads {ref}, but {target} does not exist", (rel,)))
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

    if not port:
        return
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
