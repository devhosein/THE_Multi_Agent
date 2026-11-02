"""The browser check (browser.py) on small generated sites: each one a failure the stdlib check could not see.
Needs Playwright and a Chromium-family browser; skipped otherwise (the app itself never needs them)."""
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from mission_control.integration import check_integration

try:
    import playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False

SERVER = '''
import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MENU = {menu}
REJECT = {reject}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == "/api/menu": return self._send(200, json.dumps(MENU))
        p = os.path.join(ROOT, "site", {{"/": "index.html"}}.get(self.path, self.path.strip("/")))
        if os.path.isfile(p):
            return self._send(200, open(p, encoding="utf-8").read(), "text/html" if p.endswith(".html") else "text/javascript")
        self._send(404, "{{}}")
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{{}}")
        if REJECT or not body.get("name"): return self._send(400, '{{"error": "name is required"}}')
        path = os.path.join(ROOT, "backend", "bookings.json")
        data = json.load(open(path)) if os.path.exists(path) else []
        data.append(body)
        with open(path, "w") as f: json.dump(data, f)
        self._send(201, '{{"ok": true}}')
if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), H).serve_forever()
'''

PAGE = """<!doctype html><html><head><link rel="icon" href="data:,"></head><body>
<ul id="menu-list"></ul>
<form><input name="name" required><input name="email" type="email" required><button type="submit">Book</button></form>
<p id="done"></p><script src="app.js"></script></body></html>"""

APP_JS = """fetch("/api/menu").then(r => r.json()).then(d => {{
  document.getElementById("menu-list").innerHTML = ({read}).map(i => "<li>" + i.name + "</li>").join("");
}});
document.querySelector("form").addEventListener("submit", async (e) => {{
  e.preventDefault();
  const f = e.target;
  const r = await fetch("/api/reservations", {{method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{name: f.querySelector("[name=name]").value, email: f.querySelector("[name=email]").value}})}});
  {confirm}
}});
"""
MENU = {"items": [{"name": "Espresso", "price": 3}, {"name": "Cappuccino", "price": 4}]}
THANKS = 'document.getElementById("done").textContent = r.ok ? "Booked, thank you!" : "Sorry, that did not work.";'


@unittest.skipUnless(HAVE_PLAYWRIGHT, "Playwright is not installed")
class BrowserCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        (self.root / "backend").mkdir(parents=True)
        (self.root / "site").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def site(self, *, menu=MENU, read="d.items", confirm=THANKS, reject=False, page=PAGE):
        (self.root / "backend" / "server.py").write_bytes(textwrap.dedent(
            SERVER.format(menu=json.dumps(menu), reject=reject)).encode())
        (self.root / "site" / "index.html").write_bytes(page.encode())
        (self.root / "site" / "app.js").write_bytes(APP_JS.format(read=read, confirm=confirm).encode())
        r = check_integration(self.root, browser="auto")
        if any("could not run" in n for n in r.notes):
            self.skipTest("no browser could be started: " + "; ".join(r.notes))
        self.assertTrue(any(n.startswith("browser check:") for n in r.notes), r.notes)
        return r

    def kinds(self, r):
        return sorted(p.kind for p in r.problems)

    def test_a_working_site_has_no_problems_and_the_test_booking_is_undone(self):
        r = self.site()
        self.assertEqual(r.problems, [], [p.message for p in r.problems])
        self.assertFalse((self.root / "backend" / "bookings.json").exists(), "the check's own booking must be undone")

    def test_text_nobody_can_read(self):
        """Live run cafe-35b-v8: white headings on a cream page (their dark background used an undefined variable)."""
        (self.root / "site").mkdir(exist_ok=True)
        r = self.site(page=PAGE.replace("<ul id", '<h1 style="color:#fff">A warm cup</h1>'
                                        '<p style="background:#3a2317;color:#fff">Readable</p><ul id'))
        self.assertEqual(self.kinds(r), ["low_contrast"])
        self.assertTrue(r.problems[0].minor)
        self.assertIn('<h1> "A warm cup" is rgb(255, 255, 255) on rgb(255, 255, 255)', r.problems[0].message)
        self.assertNotIn("Readable", r.problems[0].message)

    def test_data_that_never_reaches_the_page(self):
        """Live run cafe-35b-v6: the server wrapped the menu twice ({"menu": {"menu": ...}}); the page stayed empty."""
        r = self.site(menu={"menu": MENU}, read="d.items || []")
        self.assertEqual(self.kinds(r), ["data_missing"])
        self.assertIn("fetched GET /api/menu, but most of what it returned is not on the page", r.problems[0].message)
        self.assertIn('{"menu": {"items"', r.problems[0].message)          # the real shape, for the fixer
        self.assertEqual(r.problems[0].files, ("site/index.html", "site/app.js"))

    def test_an_error_while_submitting_the_form(self):
        """Live run cafe-35b-v6: `form.reset()` in a function where `form` did not exist."""
        r = self.site(confirm="form.reset();")
        self.assertEqual(self.kinds(r), ["js_error"])
        self.assertIn("submitting the form", r.problems[0].message)
        self.assertIn("form is not defined", r.problems[0].message)

    def test_a_form_the_server_rejects(self):
        r = self.site(reject=True)
        self.assertEqual(self.kinds(r), ["form_failed"])
        self.assertIn("HTTP 400", r.problems[0].message)
        self.assertIn("backend/server.py", r.problems[0].files)

    def test_a_form_that_shows_nothing_in_return(self):
        r = self.site(confirm="")
        self.assertEqual(self.kinds(r), ["form_failed"])
        self.assertIn("shows nothing in return", r.problems[0].message)


if __name__ == "__main__":
    unittest.main()
