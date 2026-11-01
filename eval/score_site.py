#!/usr/bin/env python3
r"""Score a generated cafe website against eval/tasks/cafe.md, the way a user would try it.

    python eval/score_site.py C:\path\to\project --label multi-agent --browser msedge

Starts the backend, opens the site in a real browser (Playwright), reads the menu, books a table through the form and
checks that the booking was stored. Everything automatic is measured, not judged; the design score (Q4) is left to a
reviewer, who gets desktop and mobile screenshots. The project is restored afterwards (stored bookings are undone).
Writes <out>/<label>/score.json and the screenshots; prints a summary. Needs: pip install playwright."""
import argparse
import ast
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright  # noqa: E402

API_PORT, STATIC_PORT = 8000, 8001
SKIP = {".mission", "node_modules", "__pycache__", ".git", "inputs"}
CONFIRM_WORDS = ("thank", "confirm", "success", "received", "reserved", "booked", "see you")


# ------------------------------------------------------------------ helpers
def files(root: Path, suffix: str):
    return [p for p in root.rglob("*" + suffix) if not (set(p.relative_to(root).parts) & SKIP)]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http(method: str, url: str, body: bytes | None = None, timeout: float = 5):
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()
    except (urllib.error.URLError, OSError) as e:
        return 0, "", str(e).encode()


def stdlib_only(path: Path, local: set[str]) -> list[str]:
    bad = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8", errors="replace"))):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module and not node.level else []
        for n in names:
            top = n.split(".")[0]
            if top not in sys.stdlib_module_names and top not in local:
                bad.append(top)
    return sorted(set(bad))


def find_backend(root: Path) -> Path | None:
    cands = []
    for p in files(root, ".py"):
        rel = p.relative_to(root).parts
        if rel[0] in ("tests", "test") or p.name.startswith("test_"):
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if "__main__" in src and ("http.server" in src or "HTTPServer" in src):
            rank = (0 if rel[0] == "backend" else 1, 0 if p.stem in ("server", "app", "main") else 1, len(rel))
            cands.append((rank, p))
    return sorted(cands)[0][1] if cands else None


def json_snapshot(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in files(root, ".json")}


def menu_items(obj) -> list[str]:
    out = []
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str) and any(k in obj for k in ("price", "cost", "price_cents")):
            out.append(obj["name"])
        for v in obj.values():
            out += menu_items(v)
    elif isinstance(obj, list):
        for v in obj:
            out += menu_items(v)
    return out


def js_endpoints(root: Path) -> list[str]:
    urls = []
    for p in files(root, ".js") + files(root, ".html"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        urls += re.findall(r"""['"`]((?:https?://[^'"`\s]+)?/api/[A-Za-z0-9_/\-]+)""", txt)
    return [re.sub(r"^https?://[^/]+", "", u) for u in urls]


def scroll_through(pg) -> None:
    """Scroll to the bottom and back like a visitor: sections that fade in on scroll are shown in the screenshot."""
    pg.evaluate("""async () => { for (let y = 0; y < document.body.scrollHeight; y += innerHeight / 2) {
        scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); } scrollTo(0, 0); }""")
    pg.wait_for_timeout(900)


# ------------------------------------------------------------------ scoring
def score(root: Path, out: Path, channel: str | None) -> dict:
    s: dict = {"project": str(root), "points": {}, "notes": {}, "procs": []}
    pts, notes = s["points"], s["notes"]
    procs: list[subprocess.Popen] = []
    snap = json_snapshot(root)
    try:
        # F1 backend starts, stdlib only
        backend = find_backend(root)
        notes["backend"] = str(backend.relative_to(root)) if backend else None
        api = None
        if backend:
            for port in (API_PORT, STATIC_PORT):
                if port_open(port):
                    raise SystemExit(f"port {port} is busy; stop whatever uses it first")
            local = {p.stem for p in backend.parent.glob("*.py")}
            bad = stdlib_only(backend, local)
            procs.append(subprocess.Popen([sys.executable, str(backend)], cwd=root, env={**os.environ, "PORT": str(API_PORT)},
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
            for _ in range(50):
                if port_open(API_PORT):
                    api = f"http://127.0.0.1:{API_PORT}"
                    break
                time.sleep(0.2)
            notes["F1"] = f"listening: {bool(api)}, non-stdlib imports: {bad or 'none'}"
            pts["F1"] = 10 if api and not bad else 5 if api else 0
        else:
            pts["F1"], notes["F1"] = 0, "no runnable http.server backend found"

        # F2 menu API
        eps = js_endpoints(root)
        menu_ep = next((e for e in ["/api/menu"] + eps if "menu" in e), "/api/menu")
        resv_ep = next((e for e in eps + ["/api/reservations"] if "reserv" in e or "book" in e), "/api/reservations")
        items: list[str] = []
        if api:
            st, ct, body = http("GET", api + menu_ep)
            try:
                items = menu_items(json.loads(body))
            except ValueError:
                pass
            notes["F2"] = f"GET {menu_ep} -> {st}, {len(items)} items with name+price"
        pts["F2"] = 10 if len(items) >= 3 else 0

        # where the pages come from
        landing_rel = next((r for r in ("index.html", "frontend/index.html", "public/index.html", "static/index.html", "web/index.html")
                            if (root / r).is_file()), None)
        base = None
        if api:
            st, ct, _ = http("GET", api + "/")
            if st == 200 and "html" in ct:
                base, landing_rel = api + "/", ""
        if base is None and landing_rel is not None:
            procs.append(subprocess.Popen([sys.executable, "-m", "http.server", str(STATIC_PORT), "--bind", "127.0.0.1"],
                                          cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(50):
                if port_open(STATIC_PORT):
                    break
                time.sleep(0.2)
            base = f"http://127.0.0.1:{STATIC_PORT}/"
        notes["served_by"] = "backend" if base and base.startswith(str(api)) else "static file server" if base else "nothing"

        errors: list[str] = []
        with sync_playwright() as p:
            b = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
            ctx = b.new_context(viewport={"width": 1366, "height": 900})
            pg = ctx.new_page()
            pg.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text[:200]}"))
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:200]}"))
            pg.on("requestfailed", lambda r: errors.append(f"failed: {r.url}"))
            pg.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status}: {r.url}"))

            def visit(url: str, shot: str) -> str:
                pg.goto(url, wait_until="load")
                try:
                    pg.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                scroll_through(pg)
                pg.screenshot(path=str(out / f"{shot}_desktop.png"), full_page=True)
                return pg.inner_text("body")

            pages: dict[str, str] = {}
            if base is None:
                pts.update(F3=0, F4=0, F5=0, F7=0, Q1=0)
                notes["F3"] = "no landing page found"
            else:
                landing = base + (landing_rel or "")
                pages["landing"] = landing
                text = visit(landing, "1_landing").lower()
                story = any(w in text for w in ("story", "about us", "our journey", "since"))
                hours = "hour" in text or re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)", text) is not None
                pts["F3"] = 5 * story + 5 * hours
                notes["F3"] = f"story: {story}, opening hours: {hours}"
                links = pg.eval_on_selector_all("a[href]", "as => as.map(a => [a.href, (a.innerText || '').toLowerCase()])")
                pick = lambda *keys: next((h for h, t in links if any(k in h.lower() or k in t for k in keys)), None)  # noqa: E731
                pages["menu"] = pick("menu") or landing
                pages["reserve"] = pick("reserv", "book") or landing

                # F4 menu shows the API's items
                text = visit(pages["menu"], "2_menu")
                shown = [n for n in items if n.lower() in text.lower()]
                pts["F4"] = 15 if items and len(shown) >= 0.8 * len(items) else 0
                notes["F4"] = f"{len(shown)}/{len(items)} menu items visible on {pages['menu'].replace(base, '/')}"

                # F5 book a table through the form
                before_text = visit(pages["reserve"], "3_reserve").lower()
                tomorrow = (date.today() + timedelta(days=3)).isoformat()
                filled = pg.evaluate("""([day]) => {
                  const form = [...document.querySelectorAll('form')].find(f => f.querySelectorAll('input,select,textarea').length >= 2);
                  if (!form) return null;
                  const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true})); };
                  for (const el of form.querySelectorAll('input,select,textarea')) {
                    const t = (el.type || '').toLowerCase(), key = (el.name + ' ' + el.id + ' ' + (el.placeholder || '')).toLowerCase();
                    if (['hidden', 'submit', 'button', 'reset'].includes(t) || el.disabled) continue;
                    if (el.tagName === 'SELECT') { const o = [...el.options].find(o => o.value); if (o) set(el, o.value); continue; }
                    if (t === 'checkbox' || t === 'radio') { if (el.required || t === 'radio') el.checked = true; continue; }
                    if (t === 'email') set(el, 'guest@example.com');
                    else if (t === 'tel' || key.includes('phone')) set(el, '+15550100');
                    else if (t === 'number') set(el, String(Math.max(Number(el.min) || 0, 2)));
                    else if (t === 'date') set(el, el.min && el.min > day ? el.min : day);
                    else if (t === 'time') set(el, '19:00');
                    else if (t === 'datetime-local') set(el, day + 'T19:00');
                    else if (el.tagName === 'TEXTAREA') set(el, 'Window seat please');
                    else if (key.includes('name')) set(el, 'Eval Guest');
                    else if (key.includes('guest') || key.includes('party') || key.includes('people')) set(el, '2');
                    else set(el, 'Eval Guest');
                  }
                  const btn = form.querySelector('[type=submit], button:not([type])');
                  if (btn) btn.click(); else form.requestSubmit();
                  return form.querySelectorAll('input,select,textarea').length;
                }""", [tomorrow])
                pg.wait_for_timeout(3500)
                after_text = pg.inner_text("body").lower()
                pg.screenshot(path=str(out / "3_reserve_after_submit_desktop.png"), full_page=True)
                stored = any(p_.is_file() and p_.read_bytes() != snap.get(p_) and b"Eval Guest" in p_.read_bytes() for p_ in files(root, ".json"))
                confirmed = any(after_text.count(w) > before_text.count(w) for w in CONFIRM_WORDS)
                pts["F5"] = 8 * stored + 7 * confirmed
                notes["F5"] = f"form fields filled: {filled}, stored in a JSON file: {stored}, confirmation shown: {confirmed}"

                # Q1 mobile: nothing scrolls sideways
                m = b.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
                mp = m.new_page()
                wide = {}
                for k, url in pages.items():
                    mp.goto(url, wait_until="load")
                    scroll_through(mp)
                    mp.screenshot(path=str(out / f"{ {'landing': '1_landing', 'menu': '2_menu', 'reserve': '3_reserve'}[k] }_mobile.png"), full_page=True)
                    wide[k] = mp.evaluate("document.documentElement.scrollWidth - innerWidth")
                m.close()
                pts["Q1"] = 5 if all(v <= 1 for v in wide.values()) else 0
                notes["Q1"] = f"extra width at 375px: {wide}"
            b.close()
        uniq = sorted(set(errors))
        pts["F7"] = 5 if base and not uniq else 0
        notes["F7"] = uniq[:12] or "none"

        # F6 invalid reservation rejected
        if api:
            st, _, _ = http("POST", api + resv_ep, b"{}")
            pts["F6"] = 5 if 400 <= st < 500 else 0
            notes["F6"] = f"POST {resv_ep} with {{}} -> {st}"
        else:
            pts["F6"] = 0
    finally:
        for pr in procs:
            pr.terminate()
            try:
                pr.wait(5)
            except subprocess.TimeoutExpired:
                pr.kill()
        for path, data in snap.items():          # undo the bookings made while scoring
            if path.read_bytes() != data:
                path.write_bytes(data)

    # Q2 the project's own tests (servers stopped first: tests often start their own)
    tests = [p for p in files(root, ".py") if p.name.startswith("test") or "tests" in p.relative_to(root).parts]
    results = {}
    for t in tests:
        try:
            r = subprocess.run([sys.executable, str(t)], cwd=root, capture_output=True, timeout=120)
            results[str(t.relative_to(root))] = r.returncode
        except subprocess.TimeoutExpired:
            results[str(t.relative_to(root))] = "timeout"
    for path, data in snap.items():
        if path.read_bytes() != data:
            path.write_bytes(data)
    pts["Q2"] = 5 if results and all(v == 0 for v in results.values()) else 0
    notes["Q2"] = results or "no tests written"

    # Q3 says how to start it
    docs = [p.read_text(encoding="utf-8", errors="replace") for p in list(root.glob("README*")) + ([backend] if backend else [])]
    pts["Q3"] = 5 if any(re.search(r"python3? +[\w/\\.-]*\.py", d) for d in docs) else 0
    notes["Q3"] = "a run command is documented" if pts["Q3"] else "no run command found"

    pts["Q4"] = None
    notes["Q4"] = "design: reviewer score 0-15 from the screenshots"
    src = [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP)]
    s["size"] = {"files": len(src), "lines": sum(len(p.read_bytes().splitlines()) for p in src)}
    s["automatic_total"] = sum(v for v in pts.values() if isinstance(v, int))
    s.pop("procs")
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--label", required=True)
    ap.add_argument("--browser", default=None, help="msedge / chrome to use an installed browser")
    ap.add_argument("--out", default="eval/results")
    a = ap.parse_args()
    out = Path(a.out) / a.label
    out.mkdir(parents=True, exist_ok=True)
    res = score(Path(a.project).resolve(), out, a.browser)
    (out / "score.json").write_bytes(json.dumps(res, indent=1, ensure_ascii=False).encode("utf-8"))
    print(f"== {a.label}: {res['automatic_total']}/85 automatic (+ design /15)")
    for k in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "Q1", "Q2", "Q3"):
        print(f"  {k} {res['points'].get(k)!s:>3}  {res['notes'].get(k)}")
    print(f"  served by: {res['notes'].get('served_by')}, size: {res['size']}")
