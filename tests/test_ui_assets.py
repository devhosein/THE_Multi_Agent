"""Static sanity checks for the UI files. They exist because a scripted edit once silently duplicated
a chunk of app.css and made the plan ticks invisible; none of the other tests could see that."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "mission_control" / "ui"
CSS = (UI / "app.css").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")
HTML = (UI / "index.html").read_text(encoding="utf-8")


def top_level_rules(css):
    """Yield (selector, body) for rules that are not inside an @-block (media/keyframes)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    depth, buf, rules, start = 0, "", [], 0
    i = 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            if depth == 0:
                selector = css[start:i].strip()
                body_start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append((re.sub(r"\s+", " ", selector), css[body_start:i]))
                start = i + 1
        i += 1
    return rules


class Css(unittest.TestCase):
    def test_no_duplicate_selectors(self):
        seen, dups = set(), []
        for sel, _ in top_level_rules(CSS):
            if sel.startswith("@"):
                continue
            if sel in seen:
                dups.append(sel)
            seen.add(sel)
        self.assertEqual(dups, [], "duplicated selectors (a chunk was probably pasted twice)")

    def test_every_animation_has_keyframes(self):
        defined = set(re.findall(r"@keyframes\s+([\w-]+)", CSS))
        used = set()
        for m in re.finditer(r"animation:\s*([^;}]+)", CSS):
            for token in re.findall(r"(?<![\w-])([a-zA-Z][\w-]*)(?![\w(-])", m.group(1)):
                if token not in {"infinite", "both", "forwards", "ease", "ease-in", "ease-out", "ease-in-out", "linear", "steps", "none", "alternate", "backwards"}:
                    used.add(token)
        self.assertEqual(used - defined, set())

    # Variables that app.js sets inline on elements (tree indentation, card entrance delay).
    SET_BY_JS = {"--depth", "--d"}

    def test_every_css_variable_is_defined(self):
        defined = set(re.findall(r"(--[\w-]+)\s*:", CSS))
        used = set(re.findall(r"var\((--[\w-]+)", CSS))
        self.assertEqual(used - defined - self.SET_BY_JS, set())

    def test_js_provided_variables_really_are_set_by_js(self):
        for name in self.SET_BY_JS:
            self.assertIn(name + ":", JS.replace(" ", ""), name)

    def test_braces_balance(self):
        self.assertEqual(CSS.count("{"), CSS.count("}"))


class Js(unittest.TestCase):
    def test_no_duplicate_function_declarations(self):
        names = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z0-9_]+)", JS, flags=re.M)
        self.assertEqual(sorted({n for n in names if names.count(n) > 1}), [])

    def test_every_selected_id_exists(self):
        in_html = set(re.findall(r'id="([\w-]+)"', HTML))
        built_in_js = set(re.findall(r"""id:\s*['"]([\w-]+)['"]""", JS))
        wanted = set(re.findall(r"""\$\('#([\w-]+)'\)""", JS)) | set(re.findall(r"""querySelector\('#([\w-]+)'\)""", JS))
        self.assertEqual(wanted - in_html - built_in_js, set())

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_syntax(self):
        for name in ("app.js", "state.js"):
            r = subprocess.run(["node", "--check", str(UI / name)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 0, r.stderr)


class Html(unittest.TestCase):
    def test_referenced_assets_exist(self):
        for ref in re.findall(r'(?:src|href)="([\w.]+\.(?:js|css))"', HTML):
            self.assertTrue((UI / ref).is_file(), ref)

    def test_ids_are_unique(self):
        ids = re.findall(r'id="([\w-]+)"', HTML)
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])

    def test_english_is_the_default_document(self):
        self.assertIn('<html lang="en" dir="ltr">', HTML)
        self.assertLess(HTML.index('src="i18n.js"'), HTML.index('href="app.css"'))   # direction set before first paint


def _node_json(expr):
    r = subprocess.run(["node", "-e", f"const I = require({json.dumps(str(UI / 'i18n.js'))}); console.log(JSON.stringify({expr}))"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class I18n(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dict = _node_json("I.DICT")

    def test_both_languages_have_exactly_the_same_keys(self):
        en, fa = set(self.dict["en"]), set(self.dict["fa"])
        self.assertEqual(en - fa, set(), "missing in Persian")
        self.assertEqual(fa - en, set(), "missing in English")

    def test_placeholders_match_between_languages(self):
        for k, v in self.dict["en"].items():
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", v)), sorted(re.findall(r"\{(\w+)\}", self.dict["fa"][k])), k)

    def test_every_literal_key_used_by_the_code_exists(self):
        used = set(re.findall(r"\bt\('([\w.]+)'", JS)) | set(re.findall(r'data-i18n(?:-ph|-aria)?="([\w.]+)"', HTML))
        used = {k for k in used if not k.endswith(".")}      # t('task.' + status): prefixes are covered by the status test
        self.assertEqual(used - set(self.dict["en"]), set())

    def test_every_status_the_ui_can_show_has_a_label(self):
        keys = set(self.dict["en"])
        for prefix, statuses in {
            "run": ["running", "pausing", "paused", "completed", "stopped", "failed", "idle"],
            "agent": ["queued", "running", "paused", "done", "verified", "error", "halted"],
            "task": ["pending", "running", "paused", "halted", "notRun", "done", "verified", "failed", "skipped", "removed"],
        }.items():
            for st in statuses:
                self.assertIn(f"{prefix}.{st}", keys)

    def test_every_user_facing_server_error_code_is_translated(self):
        src = "".join((UI.parent / f).read_text(encoding="utf-8") for f in ("server.py", "session.py", "engine.py"))
        # codes appear as code="x" or positionally right after the HTTP status: ..., 409, "run_finished"
        codes = set(re.findall(r'code="(\w+)"', src)) | set(re.findall(r'\b\d{3},\s*"([a-z_]+)"', src))
        internal = {"bad_host", "bad_origin", "not_found", "too_large", "bad_batch", "bad_filename", "bad_mode",
                    "bad_speed", "bad_command", "fs_error"}       # cannot be triggered from the normal UI
        self.assertTrue({"bad_path", "model_missing", "ollama_unreachable", "run_finished"} <= codes, codes)  # the scan works
        self.assertEqual({c for c in codes - internal if f"err.{c}" not in self.dict["en"]}, set())

    def test_the_code_itself_is_language_neutral(self):
        self.assertIsNone(re.search("[\u0600-\u06ff]", JS), "human text belongs in i18n.js, not in app.js")
        self.assertIsNone(re.search("[\u0600-\u06ff]", (UI / "state.js").read_text(encoding="utf-8")))

if __name__ == "__main__":
    unittest.main()
