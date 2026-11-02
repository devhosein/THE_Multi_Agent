"""Optional browser check: open the pages in a real browser and use them like a person would.

The standard-library integration check cannot run JavaScript. Live run cafe-35b-v6 passed it ("everything fits
together", all four tasks verified) while in a browser the menu page showed none of the menu (the server wrapped the
menu twice: {"menu": {"menu": ...}}) and the reservation form threw `ReferenceError: form is not defined`.

When Playwright and a Chromium-family browser are installed, this module loads every page the backend serves and:
  * reports JavaScript errors and failed requests (with the URL), while loading and while using the page
  * checks that data a page fetched from the API (names, titles) really appears on the page
  * fills in each form the way a person would, submits it, and checks the request reached the API, was accepted, and
    that the page showed something in return
  * finds text nobody can read: its colour (almost) equals the background behind it (live run cafe-35b-v8: white
    headings on a cream page, because the dark background used an undefined CSS variable)

The app itself stays standard-library only: without Playwright this does nothing and says so in a note. Files the
form submissions change (stored bookings) are restored by the caller (`integration.check_integration`)."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any, Callable

CHANNELS = (None, "msedge", "chrome")       # Playwright's own Chromium first, then installed browsers
NAME_KEYS = ("name", "title", "label")
_FOUND: dict[str, str | None] = {}

FILL_JS = """([day]) => {
  const form = [...document.querySelectorAll('form')].find(f => f.querySelectorAll('input,select,textarea').length >= 2);
  if (!form) return 0;
  const set = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true}));
                           el.dispatchEvent(new Event('change', {bubbles: true})); };
  let n = 0;
  for (const el of form.querySelectorAll('input,select,textarea')) {
    const t = (el.type || '').toLowerCase(), key = (el.name + ' ' + el.id + ' ' + (el.placeholder || '')).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'file'].includes(t) || el.disabled) continue;
    n++;
    if (el.tagName === 'SELECT') { const o = [...el.options].find(o => o.value); if (o) set(el, o.value); continue; }
    if (t === 'checkbox' || t === 'radio') { if (el.required || t === 'radio') el.checked = true; continue; }
    if (t === 'email') set(el, 'guest@example.com');
    else if (t === 'tel' || key.includes('phone')) set(el, '+15550100');
    else if (t === 'number') set(el, String(Math.max(Number(el.min) || 0, 2)));
    else if (t === 'date') set(el, el.min && el.min > day ? el.min : day);
    else if (t === 'time') set(el, '19:00');
    else if (t === 'datetime-local') set(el, day + 'T19:00');
    else if (t === 'url') set(el, 'https://example.com');
    else if (el.tagName === 'TEXTAREA') set(el, 'A test message');
    else if (key.includes('guest') || key.includes('party') || key.includes('people') || key.includes('size')) set(el, '2');
    else set(el, 'Test Guest');
  }
  const btn = form.querySelector('[type=submit], button:not([type])');
  if (btn) btn.click(); else form.requestSubmit();
  return n;
}"""


# Text whose colour is (almost) the colour behind it. Elements over a background image or gradient are skipped
# (the real background is unknown), and so are invisible ones (scroll-reveal effects start at opacity 0).
CONTRAST_JS = r"""() => {
  const rgb = c => { const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) return null; const p = m[1].split(',').map(Number);
                     return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const lum = c => { const f = v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); };
                     return .2126 * f(c.r) + .7152 * f(c.g) + .0722 * f(c.b); };
  const behind = el => { for (let e = el; e; e = e.parentElement) { const s = getComputedStyle(e);
      if (s.backgroundImage !== 'none') return null; const c = rgb(s.backgroundColor); if (c && c.a > .5) return c; }
    return {r: 255, g: 255, b: 255, a: 1}; };
  const out = [];
  for (const el of document.querySelectorAll('h1,h2,h3,h4,p,a,li,button,label,span,td,th,small,strong')) {
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim();
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    if (text.length < 2 || !r.width || !r.height || s.visibility === 'hidden') continue;
    let o = 1; for (let e = el; e; e = e.parentElement) o *= +getComputedStyle(e).opacity; if (o < .5) continue;
    const fg = rgb(s.color), bg = behind(el); if (!fg || !bg || fg.a < .5) continue;
    const a = lum(fg), b = lum(bg), ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    if (ratio < 1.6) out.push(`<${el.tagName.toLowerCase()}> "${text.slice(0, 40)}" is ${s.color} on ${s.backgroundColor === 'rgba(0, 0, 0, 0)' ? 'rgb(' + [bg.r, bg.g, bg.b].join(', ') + ')' : s.backgroundColor} (contrast ${ratio.toFixed(2)})`);
  }
  return out;
}"""


def setting() -> str:
    """MC_BROWSER_CHECK: auto (default) | off | a browser channel such as msedge."""
    return os.environ.get("MC_BROWSER_CHECK", "auto").strip().lower() or "auto"


def _launch(p: Any, choice: str):
    """Start the first browser that works; remember which one for the next check."""
    if choice in _FOUND:
        ch = _FOUND[choice]
        return p.chromium.launch(channel=ch) if ch else p.chromium.launch()
    order = CHANNELS if choice == "auto" else (choice,)
    for ch in order:
        try:
            b = p.chromium.launch(channel=ch) if ch else p.chromium.launch()
        except Exception:  # noqa: BLE001 - not installed: try the next one
            continue
        _FOUND[choice] = ch
        return b
    raise RuntimeError("no browser could be started (tried " + ", ".join(c or "chromium" for c in order) + ")")


def _names(data: Any, out: list[str]) -> list[str]:
    """Values a page should show: strings under name/title/label keys, anywhere in the JSON."""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str) and k.lower() in NAME_KEYS and 2 < len(v.strip()) <= 80:
                out.append(v.strip())
            else:
                _names(v, out)
    elif isinstance(data, list):
        for v in data:
            _names(v, out)
    return out


def check_pages(port: int, pages: dict[str, str], files_of: Callable[[str], tuple[str, ...]], backend: str | None,
                problem: Callable[..., None], note: Callable[[str], None],
                choice: str | None = None) -> None:
    """`pages`: page file -> URL path on the backend. `files_of(page)`: the page and the scripts it loads (to route a
    problem to the agent that owns them). `problem(kind, message, files, minor=False)`. Never raises."""
    choice = choice or setting()
    if choice == "off" or not pages:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        note("browser check skipped: Playwright is not installed (pip install playwright)")
        return
    base = f"http://127.0.0.1:{port}"
    day = (date.today() + timedelta(days=3)).isoformat()
    try:
        with sync_playwright() as p:
            browser = _launch(p, choice)
            note(f"browser check: {_FOUND.get(choice) or 'chromium'}")
            try:
                for rel, path in pages.items():
                    _one_page(browser, base, rel, path, day, files_of(rel), backend, problem)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - a broken browser must never stop the run
        note(f"browser check could not run: {type(e).__name__}: {str(e)[:200]}")


def _one_page(browser: Any, base: str, rel: str, path: str, day: str, files: tuple[str, ...], backend: str | None,
              problem: Callable[..., None]) -> None:
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    events: list[str] = []
    api: list[tuple[str, str, int, str]] = []          # (method, path, status, body)

    def on_response(r: Any) -> None:
        url = r.url.replace(base, "")
        if r.status >= 400 and not url.startswith("/api"):       # API answers are judged below, where they matter
            events.append(f"HTTP {r.status} for {url}")
        if url.startswith("/api"):
            try:
                body = r.text()[:20_000]
            except Exception:  # noqa: BLE001 - a body we cannot read is still a call
                body = ""
            api.append((r.request.method, url.split("?")[0], r.status, body))

    page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
            and events.append(f"console error: {m.text[:300]}"))
    page.on("pageerror", lambda e: events.append(f"uncaught {str(e)[:300]}"))
    page.on("requestfailed", lambda r: events.append(f"request failed: {r.url.replace(base, '')}"))
    page.on("response", on_response)
    try:
        page.goto(base + path, wait_until="load", timeout=10_000)
        _settle(page)
        events += [f"HTTP {s} for {m} {u}: {b[:200]}" for m, u, s, b in api if s >= 400]
        if events:
            problem("js_error", f"{rel} (opened at {path}) has errors in the browser while loading: "
                    + "; ".join(dict.fromkeys(events)), files)
        text = page.inner_text("body")
        unreadable = page.evaluate(CONTRAST_JS)
        if unreadable:
            problem("low_contrast", f"{rel} has text nobody can read, its colour is (almost) the colour behind it: "
                    + "; ".join(unreadable[:5]) + (f" (and {len(unreadable) - 5} more)" if len(unreadable) > 5 else "")
                    + ". Often a background that did not apply (an undefined CSS variable, a missing class).",
                    files, True)
        for method, url, status, body in api:
            if method != "GET" or status != 200:
                continue
            try:
                names = _names(json.loads(body), [])
            except ValueError:
                continue
            shown = [n for n in names if n.lower() in text.lower()]
            if len(names) >= 2 and len(shown) < len(names) / 2:      # a word may also occur in other text
                problem("data_missing", f"{rel} fetched GET {url}, but most of what it returned is not on the page "
                        f"(missing: {', '.join(repr(n) for n in [n for n in names if n not in shown][:4])}). The server answered: "
                        f"{body[:400]}. Check that the script reads this exact shape and renders it into an element "
                        "the page has.", files)
        # use the first real form like a person would
        events.clear()
        before_api = len(api)
        filled = page.evaluate(FILL_JS, [day])
        if filled:
            _settle(page, 4000)
            sent = [a for a in api[before_api:] if a[0] in ("POST", "PUT", "PATCH")]
            after = page.inner_text("body") if not page.is_closed() else ""
            if events:
                problem("js_error", f"submitting the form on {rel} (all {filled} fields filled in) causes errors in "
                        "the browser: " + "; ".join(dict.fromkeys(events)), files)
            if not sent:
                problem("form_failed", f"submitting the form on {rel} (all {filled} fields filled in with valid "
                        "values) sent nothing to the API", files)
            elif any(s >= 400 for _, _, s, _ in sent):
                m, u, s, b = next(a for a in sent if a[2] >= 400)
                problem("form_failed", f"the form on {rel} sent {m} {u} with valid values in all {filled} fields, and "
                        f"the server rejected it with HTTP {s}: {b[:300]}. The form's field names and the API must "
                        "agree (see the contract).", files + ((backend,) if backend else ()))
            elif not events and after.strip() == text.strip() and page.url.endswith(path):
                problem("form_failed", f"the form on {rel} was accepted by the server ({sent[0][0]} {sent[0][1]} -> "
                        f"{sent[0][2]}), but the page shows nothing in return: tell the person it worked", files)
    finally:
        ctx.close()


def _settle(page: Any, extra_ms: int = 0) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:  # noqa: BLE001 - a page that keeps polling is not an error
        pass
    if extra_ms:
        page.wait_for_timeout(min(extra_ms, 1500))
