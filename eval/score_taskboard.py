#!/usr/bin/env python3
r"""Score a generated task board against eval/tasks/taskboard.md, the way a user would try it.

    python eval/score_taskboard.py C:\path\to\project --label mc-taskboard-1 --browser msedge

Starts the backend, opens the app in a real browser (Playwright), adds a task through the form, marks it done, deletes
it, and checks the JSON files at each step. The project is restored afterwards. Writes <out>/<label>/score.json and
screenshots for the design review (Q4). Shares its helpers with score_site.py (the cafe scorer)."""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_site import (API_PORT, SKIP, STATIC_PORT, files, find_backend, http, json_snapshot, port_open,  # noqa: E402
                        scroll_through, stdlib_only)
from playwright.sync_api import sync_playwright  # noqa: E402

DONE_RE = r"done|complete|finish|mark|check|toggle|✓|✔|☐|☑"
DELETE_RE = r"delete|remove|trash|discard|×|✕|✖|🗑"


def js_endpoints(root: Path) -> list[str]:
    urls = []
    for p in files(root, ".js") + files(root, ".html"):
        urls += re.findall(r"""['"`]((?:https?://[^'"`\s]+)?/api/[A-Za-z0-9_/\-]*)""", p.read_text(encoding="utf-8", errors="replace"))
    return [re.sub(r"^https?://[^/]+", "", u).rstrip("/") for u in urls]


def stored(root: Path, title: str) -> list[dict]:
    """Every JSON object (in any JSON file of the project) that holds the title."""
    found: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if any(isinstance(v, str) and v == title for v in o.values()):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for p in files(root, ".json"):
        try:
            walk(json.loads(p.read_bytes()))
        except ValueError:
            pass
    return found


# The item that shows `title`: climb from the text until an element also holds a control; then click a control of
# the requested kind there (a checkbox or a button/link whose text, label, title or class matches).
CLICK_JS = """([title, pattern]) => {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node = null;
  while (walker.nextNode()) if (walker.currentNode.textContent.includes(title)) { node = walker.currentNode; break; }
  if (!node) return 'title not on the page';
  const re = new RegExp(pattern, 'i');
  const describe = el => [el.innerText, el.getAttribute('aria-label'), el.title, el.className, el.name, el.value,
                          el.dataset ? Object.values(el.dataset).join(' ') : ''].join(' ');
  for (let el = node.parentElement; el && el !== document.body; el = el.parentElement) {
    const controls = [...el.querySelectorAll('button, a, input[type=checkbox], [role=button], [role=checkbox]')];
    if (!controls.length) continue;
    const hit = controls.find(c => (c.type === 'checkbox' || c.getAttribute('role') === 'checkbox')
                                   ? /done|complete|finish|mark|check|toggle/.test(pattern) : re.test(describe(c)));
    if (hit) { hit.click(); return 'clicked: ' + (hit.innerText || hit.getAttribute('aria-label') || hit.type || hit.tagName).trim().slice(0, 40); }
    if (el.querySelectorAll('*').length > 400) break;
  }
  return 'no matching control near the task';
}"""


def score(root: Path, out: Path, channel: str | None) -> dict:
    s: dict = {"project": str(root), "points": {}, "notes": {}}
    pts, notes = s["points"], s["notes"]
    procs: list[subprocess.Popen] = []
    snap = json_snapshot(root)
    title = f"Eval task {random.randint(1000, 9999)}"
    try:
        backend = find_backend(root)
        notes["backend"] = str(backend.relative_to(root)) if backend else None
        api = None
        if backend:
            for port in (API_PORT, STATIC_PORT):
                if port_open(port):
                    raise SystemExit(f"port {port} is busy; stop whatever uses it first")
            bad = stdlib_only(backend, {p.stem for p in backend.parent.glob("*.py")})
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

        # F2 the list endpoint
        eps = js_endpoints(root)
        list_ep = next((e for e in eps + ["/api/tasks"] if "task" in e and e.count("/") == 2), "/api/tasks")
        if api:
            st, _, body = http("GET", api + list_ep)
            try:
                data = json.loads(body)
                ok = isinstance(data, list) or (isinstance(data, dict) and any(isinstance(v, list) for v in data.values()))
            except ValueError:
                ok = False
            pts["F2"] = 10 if st == 200 and ok else 0
            notes["F2"] = f"GET {list_ep} -> {st}, JSON list: {ok}"
        else:
            pts["F2"] = 0

        # where the page comes from
        page_rel = next((r for r in ("index.html", "frontend/index.html", "public/index.html", "static/index.html",
                                     "web/index.html") if (root / r).is_file()), None)
        base = None
        if api:
            st, ct, _ = http("GET", api + "/")
            if st == 200 and "html" in ct:
                base, page_rel = api + "/", ""
        if base is None and page_rel is not None:
            procs.append(subprocess.Popen([sys.executable, "-m", "http.server", str(STATIC_PORT), "--bind", "127.0.0.1"],
                                          cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(50):
                if port_open(STATIC_PORT):
                    break
                time.sleep(0.2)
            base = f"http://127.0.0.1:{STATIC_PORT}/"
        notes["served_by"] = "backend" if base and api and base.startswith(api) else "static file server" if base else "nothing"

        errors: list[str] = []
        for k in ("F3", "F4", "F5", "F7", "Q1"):
            pts[k] = 0
        if base:
            with sync_playwright() as p:
                b = p.chromium.launch(channel=channel) if channel else p.chromium.launch()
                pg = b.new_context(viewport={"width": 1366, "height": 900}).new_page()
                pg.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text[:200]}"))
                pg.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:200]}"))
                pg.on("requestfailed", lambda r: errors.append(f"failed: {r.url}"))
                pg.on("response", lambda r: r.status >= 400 and errors.append(f"HTTP {r.status}: {r.url}"))
                pg.on("dialog", lambda d: d.accept())                 # "Delete this task?" confirmations
                url = base + (page_rel or "")
                pg.goto(url, wait_until="load")
                try:
                    pg.wait_for_load_state("networkidle", timeout=6000)
                except Exception:  # noqa: BLE001
                    pass
                scroll_through(pg)
                pg.screenshot(path=str(out / "1_board_desktop.png"), full_page=True)

                # F3 a form with a text field
                form = pg.evaluate("""() => { const f = [...document.querySelectorAll('form')].find(f =>
                    f.querySelector('input[type=text], input:not([type]), textarea'));
                    return f ? f.querySelectorAll('input,select,textarea,button').length : 0; }""")
                pts["F3"] = 10 if form else 0
                notes["F3"] = f"a form with a text field: {bool(form)}"

                # F4 add a task
                if form:
                    pg.evaluate("""([title]) => {
                      const f = [...document.querySelectorAll('form')].find(f => f.querySelector('input[type=text], input:not([type]), textarea'));
                      const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true}));
                                               el.dispatchEvent(new Event('change', {bubbles: true})); };
                      let titled = false;
                      for (const el of f.querySelectorAll('input,select,textarea')) {
                        const t = (el.type || '').toLowerCase();
                        if (['hidden', 'submit', 'button', 'reset', 'checkbox', 'radio'].includes(t) || el.disabled) continue;
                        if (el.tagName === 'SELECT') { const o = [...el.options].find(o => /high/i.test(o.text + o.value)) ||
                                                                 [...el.options].find(o => o.value); if (o) set(el, o.value); continue; }
                        if (t === 'date') { set(el, new Date(Date.now() + 3 * 864e5).toISOString().slice(0, 10)); continue; }
                        if (t === 'number') { set(el, '1'); continue; }
                        if (!titled && (t === 'text' || t === '' || el.tagName === 'TEXTAREA')) { set(el, title); titled = true; continue; }
                        if (el.required && !el.value) set(el, 'A note');
                      }
                      const radio = f.querySelector('input[type=radio][value*="high" i]') || f.querySelector('input[type=radio]');
                      if (radio) radio.checked = true;
                      const btn = f.querySelector('[type=submit], button:not([type])');
                      if (btn) btn.click(); else f.requestSubmit();
                    }""", [title])
                    pg.wait_for_timeout(2500)
                    on_page = title in pg.inner_text("body")
                    saved = stored(root, title)
                    pts["F4"] = 8 * on_page + 7 * bool(saved)
                    notes["F4"] = f"shown: {on_page}, stored: {bool(saved)}"
                    pg.screenshot(path=str(out / "2_board_after_add_desktop.png"), full_page=True)

                    # F5 mark done, then delete
                    if on_page:
                        before = json.dumps(saved, sort_keys=True)
                        clicked = pg.evaluate(CLICK_JS, [title, DONE_RE])
                        pg.wait_for_timeout(2000)
                        after = stored(root, title)
                        done = bool(after) and json.dumps(after, sort_keys=True) != before
                        pg.screenshot(path=str(out / "3_board_after_done_desktop.png"), full_page=True)
                        clicked_del = pg.evaluate(CLICK_JS, [title, DELETE_RE])
                        pg.wait_for_timeout(2500)
                        gone_page = title not in pg.inner_text("body")
                        gone_file = not stored(root, title)
                        pts["F5"] = 7 * done + 4 * gone_page + 4 * gone_file
                        notes["F5"] = (f"done: {clicked} -> stored state changed: {done}; delete: {clicked_del} -> "
                                       f"gone from page: {gone_page}, from file: {gone_file}")

                # Q1 mobile
                m = b.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
                mp = m.new_page()
                mp.goto(url, wait_until="load")
                scroll_through(mp)
                mp.screenshot(path=str(out / "1_board_mobile.png"), full_page=True)
                wide = mp.evaluate("document.documentElement.scrollWidth - innerWidth")
                m.close()
                pts["Q1"] = 5 if wide <= 1 else 0
                notes["Q1"] = f"extra width at 375px: {wide}"
                b.close()
            uniq = sorted(set(errors))
            pts["F7"] = 5 if not uniq else 0
            notes["F7"] = uniq[:12] or "none"
        else:
            notes["F3"] = "no page found"

        # F6 an empty task is rejected
        if api:
            post_ep = next((e for e in eps if "task" in e and e.count("/") == 2), list_ep)
            st, _, _ = http("POST", api + post_ep, b"{}")
            pts["F6"] = 5 if 400 <= st < 500 else 0
            notes["F6"] = f"POST {post_ep} with {{}} -> {st}"
        else:
            pts["F6"] = 0
    finally:
        for pr in procs:
            pr.terminate()
            try:
                pr.wait(5)
            except subprocess.TimeoutExpired:
                pr.kill()
        restore(root, snap)

    # Q2 the project's own tests
    tests = [p for p in files(root, ".py") if p.name.startswith("test") or "tests" in p.relative_to(root).parts]
    results = {}
    for t in tests:
        try:
            r = subprocess.run([sys.executable, str(t)], cwd=root, capture_output=True, timeout=120)
            results[str(t.relative_to(root))] = r.returncode
        except subprocess.TimeoutExpired:
            results[str(t.relative_to(root))] = "timeout"
    restore(root, snap)
    pts["Q2"] = 5 if results and all(v == 0 for v in results.values()) else 0
    notes["Q2"] = results or "no tests written"

    docs = [p.read_text(encoding="utf-8", errors="replace") for p in list(root.glob("README*")) + ([backend] if backend else [])]
    pts["Q3"] = 5 if any(re.search(r"python3? +[\w/\\.-]*\.py", d) for d in docs) else 0
    notes["Q3"] = "a run command is documented" if pts["Q3"] else "no run command found"
    pts["Q4"] = None
    notes["Q4"] = "design: reviewer score 0-15 from the screenshots"
    src = [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP)]
    s["size"] = {"files": len(src), "lines": sum(len(p.read_bytes().splitlines()) for p in src)}
    s["automatic_total"] = sum(v for v in pts.values() if isinstance(v, int))
    return s


def restore(root: Path, snap: dict[Path, bytes]) -> None:
    """Undo every change to the project's JSON files, and remove JSON files the scoring created."""
    for p in files(root, ".json"):
        if p not in snap:
            p.unlink()
    for path, data in snap.items():
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)


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
