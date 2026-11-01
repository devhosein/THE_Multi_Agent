"""The integration check on small generated projects: each one is a failure seen (or likely) in a real run."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from mission_control.integration import check_integration, find_backend

# A tiny stdlib backend in the style the agents write. Flags switch the behaviours under test.
SERVER = '''
import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
SERVE_PAGES = {pages}
CORS = {cors}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if CORS: self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers(); self.wfile.write(data)
    def do_OPTIONS(self):
        if not CORS: return self._send(501, "{{}}")
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    def do_GET(self):
        if self.path == "/api/menu": return self._send(200, json.dumps([{{"name": "Espresso", "price": 3}}]))
        if self.path == "/api/boom": return self._send(500, "{{\\"error\\": \\"KeyError: price\\"}}")
        if SERVE_PAGES:
            p = os.path.join(ROOT, (self.path.strip("/") or "index.html"))
            if os.path.isfile(p):
                ctype = "text/html" if p.endswith(".html") else "text/css" if p.endswith(".css") else "application/javascript"
                return self._send(200, open(p, encoding="utf-8").read(), ctype)
        self._send(404, "{{}}")
    def do_POST(self):
        if self.path == "/api/reservations":
            n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{{}}")
            return self._send(400 if not body else 201, "{{}}")
        self._send(404, "{{}}")
if __name__ == "__main__":
    {start}
    HTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), H).serve_forever()
'''

PAGE = """<!doctype html><html><head><link rel="stylesheet" href="{css}"></head>
<body><ul id="menu-list"></ul><p id="error" hidden></p>{extra}<script src="app.js"></script></body></html>"""

APP_JS = """const API = "{base}";
async function load() {{
  const r = await fetch(`${{API}}/api/menu`);
  document.getElementById("menu-list").innerHTML = (await r.json()).map(i => `<li>${{i.name}}</li>`).join("");
}}
async function book() {{
  await fetch(`${{API}}/api/reservations`, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: "{{}}"}});
}}
document.getElementById("error").hidden = true;
load();
"""


class IntegrationCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        (self.root / "backend").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def project(self, *, pages=True, cors=False, base="", css="style.css", make_css=True, extra="", js=None, start="pass"):
        def w(rel, text):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(textwrap.dedent(text).encode())
        w("backend/server.py", SERVER.format(pages=pages, cors=cors, start=start))
        w("index.html", PAGE.format(css=css, extra=extra))
        w("app.js", js if js is not None else APP_JS.format(base=base))
        if make_css:
            w("style.css", "body { color: #4a2c1a; }")
        return check_integration(self.root)

    def kinds(self, report):
        return sorted(p.kind for p in report.problems)

    # ---------------------------------------------------------------- healthy
    def test_backend_serving_pages_with_relative_urls_fits_together(self):
        r = self.project(pages=True, base="")
        self.assertTrue(r.ran)
        self.assertEqual(r.served_by, "backend")
        self.assertEqual(r.problems, [], [p.message for p in r.problems])
        self.assertIn("everything fits together", r.summary())

    def test_an_api_on_another_origin_with_cors_is_fine(self):
        r = self.project(pages=False, cors=True, base="http://localhost:8000")
        self.assertEqual(self.kinds(r), [], [p.message for p in r.problems])

    # ---------------------------------------------------------------- the real failure (cafe-35b)
    def test_cross_origin_without_cors_is_found_for_get_and_post(self):
        r = self.project(pages=False, cors=False, base="http://localhost:8000")
        self.assertEqual(r.served_by, "files")
        cross = [p for p in r.problems if p.kind == "cross_origin"]
        self.assertEqual(sorted(p.message.split("(")[1].split(")")[0] for p in cross), ["GET", "POST"])
        self.assertIn("backend/server.py", cross[0].files)
        self.assertIn("app.js", cross[0].files)
        self.assertIn("serve the pages from the backend", cross[0].message)

    def test_relative_urls_while_the_backend_does_not_serve_the_pages(self):
        r = self.project(pages=False, base="")
        self.assertIn("relative_api", self.kinds(r))

    # ---------------------------------------------------------------- broken pieces
    def test_a_route_the_frontend_calls_but_the_backend_lacks(self):
        js = 'fetch("/api/specials").then(r => r.json());'
        r = self.project(js=js)
        self.assertEqual(self.kinds(r), ["api_missing"])
        self.assertIn("/api/specials", r.problems[0].message)

    def test_a_server_error_is_reported_with_its_body(self):
        r = self.project(js='fetch("/api/boom");')
        self.assertEqual(self.kinds(r), ["api_error"])
        self.assertIn("KeyError", r.problems[0].message)

    def test_a_missing_stylesheet_and_a_missing_background_image(self):
        r = self.project(css="css/missing.css", make_css=False,
                         extra='<div style="background-image: url(\'img/coffee.jpg\')"></div>')
        msgs = " ".join(p.message for p in r.problems)
        self.assertEqual(self.kinds(r), ["asset_missing", "asset_missing"])
        self.assertIn("css/missing.css", msgs)
        self.assertIn("img/coffee.jpg", msgs)

    def test_an_element_the_script_needs_but_the_page_lacks(self):
        js = 'document.getElementById("menu-container").innerHTML = "x";'
        r = self.project(js=js)
        self.assertEqual(self.kinds(r), ["dom_id"])
        self.assertIn("#menu-container", r.problems[0].message)
        self.assertEqual(r.problems[0].files, ("app.js", "index.html"))

    def test_guarded_lookups_and_elements_the_script_creates_are_not_problems(self):
        js = """const b = document.getElementById("hero-cta"); if (b) b.addEventListener("click", () => {});
                document.body.insertAdjacentHTML("beforeend", '<div id="toast"></div>');
                document.getElementById("toast").textContent = "hi";"""
        r = self.project(js=js)
        self.assertEqual(r.problems, [], [p.message for p in r.problems])

    def test_a_server_that_crashes_at_start(self):
        r = self.project(start='raise RuntimeError("menu.json not found")')
        self.assertEqual(self.kinds(r)[0], "server_start")
        self.assertIn("menu.json not found", r.problems[0].message)

    # ---------------------------------------------------------------- scope
    def test_pages_in_a_nested_folder_served_from_the_root_are_found(self):
        """Live run cafe-35b-v4: pages in backend/frontend/, served at /index.html; only one folder was stripped."""
        server = SERVER.format(pages=True, cors=False, start="pass").replace(
            'ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))',
            'ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "site")')
        (self.root / "backend" / "server.py").write_bytes(textwrap.dedent(server).encode())
        site = self.root / "backend" / "web" / "site"
        site.mkdir(parents=True)
        (site / "index.html").write_bytes(PAGE.format(css="style.css", extra="").encode())
        (site / "style.css").write_bytes(b"body { color: #4a2c1a; }")
        (site / "app.js").write_bytes(APP_JS.format(base="").encode())
        r = check_integration(self.root)
        self.assertEqual(r.served_by, "backend")
        self.assertEqual(r.problems, [], [p.message for p in r.problems])

    def test_nothing_to_check_without_pages_or_a_server(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        (empty / "notes.md").write_text("# notes")
        r = check_integration(empty)
        self.assertFalse(r.ran)
        self.assertEqual(r.problems, [])

    def test_the_backend_is_the_server_not_a_test_file(self):
        self.project()
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_x.py").write_text("import http.server\nif __name__ == '__main__': pass\n")
        self.assertEqual(find_backend(self.root).name, "server.py")

    def test_the_server_is_stopped_afterwards(self):
        import socket
        r = self.project()
        with socket.socket() as s:
            s.settimeout(0.3)
            self.assertNotEqual(s.connect_ex(("127.0.0.1", r.port)), 0, "the backend is still listening")


if __name__ == "__main__":
    unittest.main()
