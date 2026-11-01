import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from mission_control.eventlog import read_events
from mission_control.events import Event, T, check_stream
from mission_control.server import make_server

from .helpers import APP_PY, FINISH, PLAN3, OllamaStandIn, ScriptedLLM, call, happy_workers


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        ui = self.base / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
        (ui / "app.js").write_text("// js", encoding="utf-8")
        (self.base / "secret.txt").write_text("nope", encoding="utf-8")
        self.httpd = self.build_server(ui)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.project = self.base / "proj"

    def build_server(self, ui):
        return make_server("127.0.0.1", 0, ui_dir=ui)

    def tearDown(self):
        s = self.httpd.app.session
        if s:
            s.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    # --------------------------------------------------------------- helpers
    def req(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        try:
            return r.status, json.loads(payload)
        except ValueError:
            return r.status, payload

    def run_project(self, speed=0, **extra):
        return self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "سایت کافه", "speed": speed, **extra})

    def sse(self, path="/api/events?after=0", headers=None, until=T.RUN_FINISHED, limit=10):
        """Read an SSE stream until an event of type `until` arrives. Returns (events, saw_ready)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=limit)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        events, ready, name, data = [], False, None, []
        deadline = time.time() + limit
        while time.time() < deadline:
            line = r.fp.readline().decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "":
                if name == "ready":
                    ready = True
                elif name == "ev" and data:
                    events.append(Event.from_json("".join(data)))
                    if events[-1].type == until:
                        break
                name, data = None, []
        c.close()
        return events, ready

    def wait_for(self, predicate, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False


class Basics(ServerCase):
    def test_static_files_and_traversal(self):
        s, body = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"<title>x</title>", body)
        self.assertEqual(self.req("GET", "/app.js")[0], 200)
        for bad in ("/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/sub/app.js", "/.hidden", "/nope.js"):
            self.assertEqual(self.req("GET", bad)[0], 404, bad)

    def test_foreign_host_and_origin_are_rejected(self):
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})[0], 200)
        s, _ = self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "x"},
                        headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        self.assertFalse(self.project.exists())  # nothing happened
        s, _ = self.req("POST", "/api/reset", {}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s, 200)

    def test_status_before_and_after(self):
        self.assertEqual(self.req("GET", "/api/status")[1]["session"], False)
        self.run_project(speed=0)
        self.assertEqual(self.req("GET", "/api/status")[1]["session"], True)

    def test_run_validation(self):
        self.assertEqual(self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "  "})[0], 400)
        self.assertEqual(self.req("POST", "/api/run", {"project_path": "", "prompt": "x"})[0], 400)
        s, body = self.req("POST", "/api/run", {"project_path": "relative/dir", "prompt": "x"})
        self.assertEqual(s, 400)
        self.assertEqual(body["code"], "bad_path")
        self.assertIn("full path", body["error"])
        self.assertEqual(body["params"]["path"], "relative/dir")
        self.assertEqual(self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "x", "speed": "fast"})[0], 400)
        s, body = self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "x", "mode": "turbo"})
        self.assertEqual((s, body["code"]), (400, "bad_mode"))
        self.assertEqual(self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": ""})[1]["code"], "empty_prompt")
        self.assertEqual(self.req("POST", "/api/run", {"project_path": "  ", "prompt": "x"})[1]["code"], "empty_path")
        self.assertFalse(self.project.exists())
        self.assertEqual(self.req("POST", "/api/run", raw=b"{broken", headers={})[0], 400)
        self.assertEqual(self.req("POST", "/api/message", {"target": "a", "text": "x"})[0], 409)  # no session

    def test_folder_picker(self):
        (self.base / "alpha").mkdir()
        (self.base / ".hidden").mkdir()
        s, body = self.req("GET", "/api/fs?path=" + quote(str(self.base)))
        self.assertEqual(s, 200)
        names = [d["name"] for d in body["dirs"]]
        self.assertIn("alpha", names)
        self.assertNotIn(".hidden", names)
        self.assertNotIn("secret.txt", names)  # files are never listed
        self.assertEqual(body["parent"], str(self.base.parent))
        self.assertEqual(self.req("GET", "/api/fs?path=" + quote(str(self.base / "secret.txt")))[0], 400)
        self.assertEqual(self.req("GET", "/api/fs?path=" + quote(str(self.base / "ghost")))[0], 400)
        self.assertEqual(self.req("GET", "/api/fs")[0], 200)  # defaults to home


class RunAndStream(ServerCase):
    def test_full_run_streams_a_valid_complete_log(self):
        s, body = self.run_project(speed=0)
        self.assertEqual(s, 200, body)
        events, ready = self.sse()
        self.assertTrue(ready)
        self.assertEqual(events[-1].type, T.RUN_FINISHED)
        self.assertEqual(check_stream(events), [])
        first = events[0]
        self.assertEqual(first.type, T.RUN_STARTED)
        self.assertEqual(first.data["prompt"], "سایت کافه")
        self.assertTrue(first.data["mock"])
        self.assertEqual(Path(first.data["project_path"]), self.project.resolve())
        on_disk = read_events(self.project / ".mission" / "events.jsonl")
        self.assertEqual([e.seq for e in on_disk], [e.seq for e in events])

    def test_resume_after_seq_via_param_and_last_event_id(self):
        self.run_project(speed=0)
        all_events, _ = self.sse()
        n = all_events[-1].seq
        part, _ = self.sse("/api/events?after=50", until=T.RUN_FINISHED)
        self.assertEqual([e.seq for e in part], list(range(51, n + 1)))
        part2, _ = self.sse("/api/events", headers={"Last-Event-ID": "100"})
        self.assertEqual(part2[0].seq, 101)

    def test_live_stream_while_running(self):
        self.run_project(speed=40)  # fast but not instant -> the stream must follow live events
        events, ready = self.sse(limit=20)
        self.assertEqual(events[-1].type, T.RUN_FINISHED)
        self.assertEqual(check_stream(events), [])

    def test_second_run_is_refused_while_running_then_allowed_after_reset(self):
        self.run_project(speed=0.5)  # slow -> still running
        s, body = self.run_project(speed=0)
        self.assertEqual(s, 409)
        self.assertEqual(self.req("POST", "/api/reset", {})[0], 200)
        self.assertEqual(self.run_project(speed=0)[0], 200)
        events, _ = self.sse()
        self.assertEqual(check_stream(events), [])
        # the first run's log was rotated, not overwritten
        self.assertEqual(len(list((self.project / ".mission").glob("events.*.jsonl"))), 1)

    def test_uploaded_files_land_in_inputs_with_safe_names(self):
        batch = "a" * 32
        name = quote("منو کافه.txt")
        s, body = self.req("POST", f"/api/upload?batch={batch}", raw="قهوه".encode("utf-8"), headers={"X-Filename": name})
        self.assertEqual((s, body["name"], body["size"]), (200, "منو کافه.txt", len("قهوه".encode("utf-8"))))
        s, body = self.req("POST", f"/api/upload?batch={batch}", raw=b"x", headers={"X-Filename": quote("../../evil.txt")})
        self.assertEqual((s, body["name"]), (200, "evil.txt"))
        self.assertEqual(self.req("POST", "/api/upload?batch=bad", raw=b"x", headers={"X-Filename": "a"})[0], 400)
        self.assertEqual(self.req("POST", f"/api/upload?batch={batch}", raw=b"x", headers={})[0], 400)
        s, body = self.run_project(speed=0, batch=batch)
        self.assertEqual(s, 200)
        self.assertEqual(sorted(body["inputs"]), ["evil.txt", "منو کافه.txt"])
        self.assertEqual((self.project / "inputs" / "منو کافه.txt").read_text(encoding="utf-8"), "قهوه")
        self.assertFalse((self.base / "evil.txt").exists())
        events, _ = self.sse()
        self.assertIn("inputs/منو کافه.txt", events[0].data["inputs"])


class UserActions(ServerCase):
    def test_message_to_agent_is_logged_and_answered(self):
        self.run_project(speed=40)
        self.assertTrue(self.wait_for(lambda: any(
            e.type == T.AGENT_CREATED and e.agent_id == "a_frontend" for e in self.httpd.app.session.log.read())))
        s, _ = self.req("POST", "/api/message", {"target": "a_frontend", "text": "حالت تاریک هم باشه"})
        self.assertEqual(s, 200)
        log = self.httpd.app.session.log
        self.assertTrue(self.wait_for(lambda: any(
            e.type == T.AGENT_MESSAGE and e.agent_id == "a_frontend" and "(demo)" in e.data["text"] for e in log.read())))
        msgs = [e for e in log.read() if e.type == T.AGENT_MESSAGE and e.agent_id == "a_frontend"
                and "حالت تاریک هم باشه" in e.data["text"]]
        self.assertEqual(msgs[0].data["role"], "user")

    def test_message_validation(self):
        self.run_project(speed=0)
        s, body = self.req("POST", "/api/message", {"target": "ghost", "text": "hi"})
        self.assertEqual((s, body["code"], body["params"]), (404, "no_agent", {"target": "ghost"}))
        self.assertEqual(self.req("POST", "/api/message", {"target": "orchestrator", "text": "   "})[0], 400)
        self.assertEqual(self.req("POST", "/api/message", {"target": "orchestrator", "text": "x" * 5000})[0], 400)
        self.assertEqual(self.req("POST", "/api/message", {"target": "orchestrator", "text": "سلام"})[0], 200)

    def test_pause_is_honestly_reported_as_not_applied_in_mock(self):
        self.run_project(speed=0.5)
        s, body = self.req("POST", "/api/command", {"command": "pause", "target": "all"})
        self.assertEqual(s, 200)
        self.assertFalse(body["applied"])
        self.assertIn("Demo mode", body["note"])
        self.assertEqual(self.req("POST", "/api/command", {"command": "explode", "target": "all"})[0], 400)
        self.assertEqual(self.req("POST", "/api/command", {"command": "pause", "target": "ghost"})[0], 404)

    def test_stop_ends_the_run_cleanly_and_the_log_stays_valid(self):
        self.run_project(speed=3)
        self.assertTrue(self.wait_for(lambda: self.httpd.app.session.log.last_seq > 5))
        s, body = self.req("POST", "/api/command", {"command": "stop", "target": "all"})
        self.assertEqual((s, body["applied"]), (200, True))
        events, _ = self.sse()
        self.assertEqual(events[-1].type, T.RUN_FINISHED)
        self.assertEqual(events[-1].data["status"], "stopped")
        self.assertEqual(sum(e.type == T.RUN_FINISHED for e in events), 1)
        self.assertEqual(check_stream(events), [])
        self.assertFalse(self.httpd.app.session.is_running())
        time.sleep(0.3)  # the player thread must not append anything after the stop
        self.assertEqual(self.httpd.app.session.log.read()[-1].type, T.RUN_FINISHED)


class Quoting(ServerCase):
    def test_a_pasted_windows_style_quoted_path_with_trailing_slash_is_accepted(self):
        quoted = '"' + str(self.project) + '/"'          # what Explorer's "Copy as path" + a trailing slash would give
        s, body = self.req("POST", "/api/run", {"project_path": quoted, "prompt": "x", "speed": 0})
        self.assertEqual(s, 200, body)
        self.assertEqual(Path(body["project_path"]), self.project.resolve())
        self.assertTrue((self.project / ".mission").is_dir())

    def test_the_folder_browser_accepts_quoted_paths_too(self):
        s, body = self.req("GET", "/api/fs?path=" + quote('"' + str(self.base) + '"'))
        self.assertEqual(s, 200)
        self.assertEqual(Path(body["path"]), self.base.resolve())

    def test_folder_errors_are_coded(self):
        s, body = self.req("GET", "/api/fs?path=" + quote(str(self.base / "secret.txt")))
        self.assertEqual((s, body["code"]), (400, "not_a_folder"))


class NativePicker(ServerCase):
    def build_server(self, ui):
        self.calls, self.answer = [], {"status": "ok", "path": "/picked/folder"}
        self.gate = None

        def picker(initial, title):
            self.calls.append((initial, title))
            if self.gate:
                self.gate.wait(5)
            return self.answer

        return make_server("127.0.0.1", 0, ui_dir=ui, pick_folder=picker)

    def test_ok_cancel_and_unavailable(self):
        s, body = self.req("POST", "/api/pick-folder", {"initial": '"/some/where/"', "lang": "en"})
        self.assertEqual((s, body), (200, {"path": "/picked/folder"}))
        self.assertEqual(self.calls[-1], ("/some/where", "Select the project folder"))        # cleaned before it reaches the dialog
        self.answer = {"status": "cancel"}
        self.assertEqual(self.req("POST", "/api/pick-folder", {})[1], {"cancelled": True})
        self.answer = {"status": "error", "message": "tkinter is not available"}
        s, body = self.req("POST", "/api/pick-folder", {})
        self.assertEqual((s, body["code"]), (501, "native_unavailable"))
        self.assertIn("tkinter", body["params"]["detail"])

    def test_dialog_title_follows_the_ui_language(self):
        self.req("POST", "/api/pick-folder", {"lang": "fa"})
        self.assertIn("پوشه", self.calls[-1][1])
        self.req("POST", "/api/pick-folder", {"lang": "klingon"})
        self.assertEqual(self.calls[-1][1], "Select the project folder")

    def test_only_one_dialog_at_a_time(self):
        self.gate = threading.Event()
        first = threading.Thread(target=lambda: self.req("POST", "/api/pick-folder", {}))
        first.start()
        self.assertTrue(self.wait_for(lambda: self.calls))
        s, body = self.req("POST", "/api/pick-folder", {})
        self.assertEqual((s, body["code"]), (409, "picker_busy"))
        self.gate.set()
        first.join(5)
        self.assertEqual(self.req("POST", "/api/pick-folder", {})[0], 200)       # the lock was released

    def test_a_refused_request_still_gets_its_answer(self):
        """A request refused before its body is read must still receive the 403. Closing a socket with unread data
        makes Windows reset the connection, and the client then sees an abort instead of the answer."""
        for headers in [{"Origin": "http://evil.example"}, {"Host": "evil.example"}] * 4:   # a race: try it a few times
            s, body = self.req("POST", "/api/pick-folder", raw=b"x" * 900_000,
                               headers={**headers, "Content-Type": "application/json"})
            self.assertEqual(s, 403)
            self.assertIn(body["code"], ("bad_origin", "bad_host"))
        self.assertEqual(self.calls, [])

    def test_a_foreign_web_page_cannot_open_dialogs_on_your_desktop(self):
        s, _ = self.req("POST", "/api/pick-folder", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        s, _ = self.req("POST", "/api/pick-folder", {}, headers={"Host": "evil.example"})
        self.assertEqual(s, 403)
        self.assertEqual(self.calls, [])

    @unittest.skipIf(sys.platform in ("win32", "darwin") or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
                     "a desktop is available: the real dialog would open on the screen and wait for someone")
    def test_the_real_picker_fails_politely_without_a_display(self):
        """No mocking: the genuine child process, on a machine with no tkinter/display, reports an error."""
        from mission_control.server import native_pick_folder
        r = native_pick_folder("", "x", timeout=30)
        self.assertIn(r["status"], ("error", "cancel", "ok"))                 # never raises, always a protocol answer
        if r["status"] == "error":
            self.assertTrue(r["message"])


class LiveMode(ServerCase):
    def build_server(self, ui):
        self.llm = OllamaStandIn()
        return make_server("127.0.0.1", 0, ui_dir=ui, llm_factory=lambda model, host: self.llm)

    def run_live(self, **extra):
        return self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "Build a cafe site", "mode": "live",
                                             "model": "test-model", **extra})

    def test_a_live_run_through_the_http_api(self):
        s, body = self.run_live()
        self.assertEqual((s, body["mode"]), (200, "live"), body)
        events, ready = self.sse(limit=30)
        self.assertTrue(ready)
        self.assertEqual(check_stream(events), [])
        self.assertEqual(events[0].data["mock"], False)
        self.assertEqual(events[0].data["model"], "test-model")
        self.assertEqual(events[-1].data["status"], "completed")
        self.assertTrue((self.project / "backend" / "app.py").is_file())
        self.assertEqual(self.req("GET", "/api/status")[1]["mode"], "live")

    def test_ollama_down_is_reported_before_anything_is_created(self):
        self.llm.down = True
        s, body = self.run_live()
        self.assertEqual((s, body["code"]), (502, "ollama_unreachable"))
        self.assertIn("ollama serve", body["error"])
        self.assertFalse(self.project.exists())
        self.assertFalse(self.httpd.app.session)

    def test_missing_model_lists_what_is_installed(self):
        s, body = self.run_live(model="nope:7b")
        self.assertEqual((s, body["code"]), (400, "model_missing"))
        self.assertIn("ollama pull nope:7b", body["error"])
        self.assertEqual(body["params"]["installed"], ["test-model:latest", "other:1b"])
        self.assertFalse(self.project.exists())

    def test_a_model_name_without_a_tag_matches_latest(self):
        self.assertEqual(self.run_live(model="test-model")[0], 200)

    def test_models_endpoint(self):
        s, body = self.req("GET", "/api/models")
        self.assertEqual(s, 200)
        self.assertTrue(body["reachable"])
        self.assertEqual(body["models"], ["test-model:latest", "other:1b"])
        self.assertEqual(body["default"], "ornith-1.5:9b")
        self.llm.down = True
        body = self.req("GET", "/api/models")[1]
        self.assertFalse(body["reachable"])
        self.assertIn("connection refused", body["error"])

    def test_real_pause_resume_and_messages_in_live_mode(self):
        self.llm.on_worker = lambda tid, msgs: time.sleep(0.15)          # slow enough to act mid-run
        self.run_live()
        self.assertTrue(self.wait_for(lambda: any(e.type == T.AGENT_CREATED for e in self.httpd.app.session.log.read())))
        s, body = self.req("POST", "/api/command", {"command": "pause", "target": "all"})
        self.assertEqual((s, body["applied"]), (200, True))
        self.assertIn("safe point", body["note"])
        self.assertTrue(self.wait_for(lambda: any(e.type == T.RUN_PAUSED for e in self.httpd.app.session.log.read())))
        s, body = self.req("POST", "/api/message", {"target": "orchestrator", "text": "Keep it simple"})
        self.assertEqual(s, 200)
        self.assertTrue(self.req("POST", "/api/command", {"command": "resume", "target": "all"})[1]["applied"])
        events, _ = self.sse(limit=30)
        self.assertEqual(check_stream(events), [])
        self.assertEqual(events[-1].data["status"], "completed")
        self.assertEqual([e.data["text"] for e in events if e.type == T.DECISION], ["Keep it simple"])

    def test_after_the_run_ends_messages_are_refused_with_a_code(self):
        self.run_live()
        self.sse(limit=30)
        s, body = self.req("POST", "/api/message", {"target": "orchestrator", "text": "hello?"})
        self.assertEqual((s, body["code"]), (409, "run_finished"))

    def test_stop_in_live_mode(self):
        self.llm.on_worker = lambda tid, msgs: time.sleep(0.3)
        self.run_live()
        self.assertTrue(self.wait_for(lambda: any(e.type == T.AGENT_CREATED for e in self.httpd.app.session.log.read())))
        self.assertEqual(self.req("POST", "/api/command", {"command": "stop", "target": "all"})[1]["applied"], True)
        events, _ = self.sse()
        self.assertEqual(events[-1].data["status"], "stopped")
        self.assertEqual(check_stream(events), [])

    def test_the_ui_language_selects_the_engine_language(self):
        self.run_live(lang="fa")
        events, _ = self.sse(limit=30)
        text = " ".join(e.data["text"] for e in events if e.type == T.ORCH_STATUS)
        self.assertIn("تحلیل", text)

    def test_demo_mode_follows_the_language_too(self):
        import re
        persian = re.compile("[\u0600-\u06ff]")
        self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "x", "speed": 0, "lang": "en"})
        en, _ = self.sse()
        self.req("POST", "/api/reset", {})
        self.req("POST", "/api/run", {"project_path": str(self.project) + "2", "prompt": "x", "speed": 0, "lang": "fa"})
        fa, _ = self.sse()
        msgs = lambda evs: [e.data["text"] for e in evs if e.type == T.AGENT_MESSAGE and e.data["role"] == "agent"]
        self.assertFalse(any(persian.search(m) for m in msgs(en)))
        self.assertTrue(any(persian.search(m) for m in msgs(fa)))


class GoAheadAndDecisions(ServerCase):
    def build_server(self, ui):
        self.llm = OllamaStandIn(installed=("test-model:latest", "other:1b"), sizes={"test-model:latest": "9.2B", "other:1b": "35B"})
        return make_server("127.0.0.1", 0, ui_dir=ui, llm_factory=lambda model, host: self.llm)

    def log(self):
        return self.httpd.app.session.log.read()

    def run_live(self, **extra):
        return self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "Build it", "mode": "live",
                                             "model": "test-model", **extra})

    def test_live_run_waits_for_approval_and_accepts_a_model_per_agent(self):
        self.assertEqual(self.run_live(require_approval=True)[0], 200)
        self.assertTrue(self.wait_for(lambda: any(e.type == T.APPROVAL_REQUESTED for e in self.log())))
        time.sleep(0.3)
        self.assertFalse(any(e.type == T.TASK_STATUS for e in self.log()))
        s, body = self.req("POST", "/api/approve", {"models": {"a_t2": "nope:7b"}})
        self.assertEqual((s, body["code"]), (400, "model_missing"))
        self.assertEqual(self.req("POST", "/api/approve", {"models": "x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/approve", {"models": {"a_t2": "other:1b"}})[0], 200)
        events, _ = self.sse(limit=20)
        self.assertEqual(check_stream(events), [])
        self.assertEqual([(e.agent_id, e.data["model"]) for e in events if e.type == T.AGENT_UPDATED], [("a_t2", "other:1b")])
        self.assertEqual(events[-1].data["status"], "completed")
        self.assertEqual(self.req("POST", "/api/approve", {})[1]["code"], "run_finished")

    def test_the_demo_waits_for_approval_too(self):
        self.req("POST", "/api/run", {"project_path": str(self.project), "prompt": "x", "speed": 0, "require_approval": True})
        self.assertTrue(self.wait_for(lambda: any(e.type == T.APPROVAL_REQUESTED for e in self.log())))
        time.sleep(0.3)
        self.assertFalse(any(e.type == T.TASK_STATUS for e in self.log()), "the demo must wait for the go-ahead")
        self.assertEqual(self.req("POST", "/api/resolve", {"task_id": "t1", "decision": "retry"})[1]["code"], "no_attention")
        self.req("POST", "/api/approve", {"models": {"a_backend": "other:1b"}})
        events, _ = self.sse()
        self.assertEqual(check_stream(events), [])
        self.assertEqual([e.data["model"] for e in events if e.type == T.AGENT_UPDATED], ["other:1b"])

    def test_failure_report_decision_then_retry_failed_tasks(self):
        bad = call("write_file", path="backend/app.py", content="def broken(:\n")
        self.llm.workers["t2"] = [bad, FINISH] * 3
        self.run_live(on_failure="ask", review=False)
        self.assertTrue(self.wait_for(lambda: any(e.type == T.ATTENTION_REQUIRED for e in self.log()), 15))
        att = [e for e in self.log() if e.type == T.ATTENTION_REQUIRED][0]
        self.assertEqual(att.task_id, "t2")
        self.assertEqual(self.req("POST", "/api/retry", {})[1]["code"], "cannot_retry")       # still running
        s, body = self.req("POST", "/api/resolve", {"task_id": "t2", "decision": "skip"})
        self.assertEqual(s, 200, body)
        events, _ = self.sse(limit=20)
        self.assertEqual(events[-1].data["status"], "failed")
        # the report explains what happened
        s, report = self.req("GET", "/api/report")
        self.assertEqual(s, 200)
        text = report.decode("utf-8")
        self.assertIn("# Mission Control diagnostic report", text)
        self.assertIn("syntax error", text)
        self.assertIn("the user chose **skip**", text)
        self.assertIn("| t2 |", text)
        self.assertIn("python", text)
        # retry only the failed work
        self.llm.workers["t2"] = [APP_PY, FINISH]
        s, body = self.req("POST", "/api/retry", {})
        self.assertEqual(s, 200, body)
        events, _ = self.sse(limit=20)
        self.assertEqual(check_stream(events), [])
        self.assertTrue(events[0].data["resumed"])
        self.assertEqual(events[-1].data["status"], "completed")
        self.assertEqual(self.req("POST", "/api/retry", {})[1]["code"], "cannot_retry")       # nothing left to redo

    def test_report_download_headers(self):
        self.run_live()
        self.sse(limit=20)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/report")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("Content-Type"), "text/markdown; charset=utf-8")
        self.assertIn("attachment; filename=\"mission-report-", r.getheader("Content-Disposition"))
        c.close()
        self.req("POST", "/api/reset", {})
        self.assertEqual(self.req("GET", "/api/report")[0], 409)

    def test_models_come_with_the_strength_they_would_get(self):
        body = self.req("GET", "/api/models")[1]
        self.assertEqual({d["name"]: d["strength"] for d in body["details"]}, {"test-model:latest": "small", "other:1b": "medium"})

    def test_run_options(self):
        for bad in ({"strength": "genius"}, {"num_ctx": 100}, {"num_ctx": "lots"}, {"on_failure": "panic"}):
            s, body = self.run_live(**bad)
            self.assertEqual(s, 400, bad)
        self.assertFalse(self.project.exists())
        self.run_live(num_ctx=65536, strength="large")
        events, _ = self.sse(limit=20)
        self.assertEqual(self.llm.num_ctx, 65536)
        self.assertEqual(events[0].data["settings"]["context"], "num_ctx=65536")
        self.assertEqual(events[0].data["settings"]["profile"], "large")

    def test_no_context_limit_by_default(self):
        self.run_live()
        events, _ = self.sse(limit=20)
        self.assertIsNone(self.llm.num_ctx)
        self.assertIn("Ollama default", events[0].data["settings"]["context"])


if __name__ == "__main__":
    unittest.main()
