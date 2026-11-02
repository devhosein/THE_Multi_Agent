import re
import tempfile
import unittest
from pathlib import Path

from mission_control.eventlog import EventLog
from mission_control.events import T
from mission_control.tools import MAX_FILE_BYTES, ToolBox
from mission_control.worker import compact
from mission_control.workspace import init_workspace


class ToolBoxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = init_workspace(Path(self.tmp.name) / "p")
        self.log = EventLog(self.ws.events_path, "r")
        self.box = ToolBox(self.ws, ["backend/**", "README.md"], self.log, "a1", chunk_delay_cap=0)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self):
        return [(e.type, e.data) for e in self.log.read()]

    def test_write_inside_owned_paths(self):
        r = self.box.call("write_file", {"path": "backend/app.py", "content": "x = 1\n"})
        self.assertTrue(r.startswith("OK"), r)
        self.assertEqual((self.ws.root / "backend/app.py").read_text(), "x = 1\n")
        self.assertEqual(self.box.written, ["backend/app.py"])
        self.assertTrue(self.box.call("write_file", {"path": "README.md", "content": "hi"}).startswith("OK"))

    def test_the_note_that_replaced_a_write_in_the_history_is_never_written_into_a_file(self):
        """Live runs v4, v7, v8: models imitated their compacted history and wrote "<<11949 characters written to
        backend/server.py; …>>" as the new content of server.py (v4 then looped), or appended it to style.css (v7)."""
        real = "body { color: #4a2c1a; }\n" * 20
        self.box.call("write_file", {"path": "backend/style.css", "content": real})
        history = [{"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "write_file", "arguments": {"path": "backend/style.css", "content": real}}}]},
            {"role": "tool", "tool_name": "write_file", "content": "OK"}, {"role": "assistant", "content": "next"}]
        compact(history)
        note = history[1]["content"].split("\n", 1)[1]
        old = "<<11949 characters written to backend/server.py; read_file shows the file>>"
        for tool in ("write_file", "append_file"):
            for content in (note, "\n" + note + "\n", old):
                r = self.box.call(tool, {"path": "backend/style.css", "content": content})
                self.assertTrue(r.startswith("ERROR: this is not file content"), r)
            r = self.box.call(tool, {"path": "backend/style.css"})                 # an imitated call without content
            self.assertIn("removed to save space", r)
        self.assertEqual((self.ws.root / "backend/style.css").read_text(), real)   # untouched

    def test_not_owned_is_refused_and_nothing_is_created(self):
        r = self.box.call("write_file", {"path": "frontend/x.html", "content": "<p>"})
        self.assertTrue(r.startswith("ERROR"))
        self.assertIn("backend/**", r)                      # tells the model what it MAY write
        self.assertFalse((self.ws.root / "frontend").exists())
        self.assertEqual(self.events(), [])

    def test_read_only_task_cannot_write(self):
        ro = ToolBox(self.ws, [], self.log, "a2", chunk_delay_cap=0)
        r = ro.call("write_file", {"path": "a.txt", "content": "x"})
        self.assertIn("read-only", r)

    def test_escape_attempts(self):
        for bad in ("../evil.txt", "/etc/passwd", "backend/../../evil.txt", ".mission/plan.json", "C:/Windows/x", ""):
            r = self.box.call("write_file", {"path": bad, "content": "x"})
            self.assertTrue(r.startswith("ERROR"), bad)
        self.assertFalse((self.ws.root.parent / "evil.txt").exists())
        self.assertFalse((self.ws.root / ".mission" / "plan.json").exists())

    def test_bad_arguments(self):
        self.assertTrue(self.box.call("write_file", {"path": "backend/a.py"}).startswith("ERROR"))
        self.assertTrue(self.box.call("write_file", {"path": "backend/a.py", "content": 5}).startswith("ERROR"))
        self.assertTrue(self.box.call("write_file", "not a dict").startswith("ERROR"))
        self.assertTrue(self.box.call("rm_rf", {}).startswith("ERROR: unknown tool"))
        big = "x" * (MAX_FILE_BYTES + 1)
        self.assertIn("too large", self.box.call("write_file", {"path": "backend/big.txt", "content": big}))

    def test_events_are_created_chunks_saved_and_reassemble(self):
        text = "line one\nسلام دنیا\n" * 10
        self.box.call("write_file", {"path": "backend/a.txt", "content": text})
        ev = self.events()
        self.assertEqual(ev[0], (T.FILE_CREATED, {"path": "backend/a.txt"}))
        self.assertEqual(ev[-1][0], T.FILE_SAVED)
        self.assertEqual(ev[-1][1]["size"], len(text.encode("utf-8")))
        self.assertEqual("".join(d["text"] for t, d in ev if t == T.FILE_CHUNK), text)

    def test_rewrite_is_flagged_and_replaces_content(self):
        self.box.call("write_file", {"path": "backend/a.txt", "content": "one"})
        self.box.call("write_file", {"path": "backend/a.txt", "content": "two"})
        created = [d for t, d in self.events() if t == T.FILE_CREATED]
        self.assertEqual(created, [{"path": "backend/a.txt"}, {"path": "backend/a.txt", "rewrite": True}])
        self.assertEqual((self.ws.root / "backend/a.txt").read_text(), "two")
        self.assertEqual(self.box.written, ["backend/a.txt"])          # listed once

    def test_newlines_are_not_translated(self):
        self.box.call("write_file", {"path": "backend/a.txt", "content": "a\nb\n"})
        self.assertEqual((self.ws.root / "backend/a.txt").read_bytes(), b"a\nb\n")

    def test_no_temp_file_is_left_behind(self):
        self.box.call("write_file", {"path": "backend/a.txt", "content": "x"})
        self.assertEqual([p.name for p in (self.ws.root / "backend").iterdir()], ["a.txt"])

    def test_read_file(self):
        (self.ws.root / "inputs").mkdir()
        (self.ws.root / "inputs" / "menu.txt").write_text("espresso", encoding="utf-8")
        self.assertEqual(self.box.call("read_file", {"path": "inputs/menu.txt"}), "espresso")
        self.assertIn("does not exist", self.box.call("read_file", {"path": "nope.txt"}))
        self.assertTrue(self.box.call("read_file", {"path": "../x"}).startswith("ERROR"))
        self.assertIn("decisions", self.box.call("read_file", {"path": ".mission/decisions.md"}))
        self.assertIn("only .mission/decisions.md", self.box.call("read_file", {"path": ".mission/events.jsonl"}))
        (self.ws.root / "bin.dat").write_bytes(b"\x00\x01\x02")
        self.assertIn("binary", self.box.call("read_file", {"path": "bin.dat"}))
        # a single enormous line (minified code) is cut, never returned whole
        (self.ws.root / "huge.txt").write_text("y" * 50_000)
        out = self.box.call("read_file", {"path": "huge.txt"})
        self.assertLess(len(out), 31_000)
        self.assertIn("50000 characters long", out)

    def test_long_files_are_paged_and_nothing_is_lost(self):
        box = ToolBox(self.ws, ["backend/**"], self.log, "a1", chunk_delay_cap=0, read_chars=1000)
        lines = [f"line {i:04d} " + "x" * 40 + "\n" for i in range(1, 101)]          # ~5000 chars
        (self.ws.root / "long.txt").write_bytes("".join(lines).encode())   # bytes: LF on every OS
        got, start = "", 1
        for _ in range(20):
            page = box.call("read_file", {"path": "long.txt", "start_line": start})
            body, _, hint = page.partition("\n...[lines ")
            got += body if hint else page
            if not hint:
                break
            self.assertIn("call read_file with start_line=", hint)
            start = int(re.search(r"start_line=(\d+)", hint).group(1))
        self.assertEqual(got, "".join(lines))                                          # every page joined = the file
        self.assertIn("has only 100 lines", box.call("read_file", {"path": "long.txt", "start_line": 500}))

    def test_list_files_hides_engine_and_hidden_folders(self):
        for p in ("backend/app.py", "frontend/index.html", ".git/config", "node_modules/x/y.js", "top.txt"):
            f = self.ws.root / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        out = self.box.call("list_files", {}).splitlines()
        self.assertIn("backend/", out)
        self.assertIn("backend/app.py", out)
        self.assertIn("top.txt", out)
        self.assertFalse([l for l in out if l.startswith((".mission", ".git", "node_modules"))])
        # a sub-folder listing still uses project-root-relative paths: the model can pass them straight to the other tools
        self.assertEqual(self.box.call("list_files", {"path": "backend"}).strip(), "backend/app.py")
        self.assertTrue(self.box.call("list_files", {"path": "../"}).startswith("ERROR"))
        self.assertIn("not a folder", self.box.call("list_files", {"path": "top.txt"}))

    def test_append_builds_a_file_in_parts(self):
        self.box.call("write_file", {"path": "backend/big.js", "content": "part1\n"})
        r = self.box.call("append_file", {"path": "backend/big.js", "content": "part2\n"})
        self.assertTrue(r.startswith("OK: appended"), r)
        self.box.call("append_file", {"path": "backend/new.js", "content": "fresh\n"})        # creates when missing
        self.assertEqual((self.ws.root / "backend/big.js").read_text(), "part1\npart2\n")
        self.assertEqual((self.ws.root / "backend/new.js").read_text(), "fresh\n")
        self.assertEqual(self.box.written, ["backend/big.js", "backend/new.js"])
        self.assertTrue(self.box.call("append_file", {"path": "frontend/x.js", "content": "x"}).startswith("ERROR"))
        self.assertTrue(self.box.call("append_file", {"path": "backend/x.js", "content": ""}).startswith("ERROR"))
        # the UI sees the appended text continue the existing file
        ev = [(e.type, e.data) for e in self.log.read() if e.data.get("path") == "backend/big.js"]
        self.assertEqual(ev[-1], (T.FILE_SAVED, {"path": "backend/big.js", "size": 12}))
        self.assertNotIn("rewrite", ev[-3][1] if ev[-3][0] == T.FILE_CREATED else {})

    def test_writing_the_same_content_again_is_called_out(self):
        self.box.call("write_file", {"path": "backend/a.sh", "content": "node test.js\n"})
        n = len(self.log.read())
        r = self.box.call("write_file", {"path": "backend/a.sh", "content": "node test.js\n"})
        self.assertIn("no change", r)
        self.assertIn("Do not write it again", r)
        self.assertEqual(len(self.log.read()), n)            # no fake "rewrite" animation in the UI

    def test_note_and_finish(self):
        self.assertTrue(self.box.call("note_to_lead", {"text": "contract is missing auth"}).startswith("OK"))
        self.assertEqual(self.box.notes, ["contract is missing auth"])
        self.assertTrue(self.box.call("note_to_lead", {"text": " "}).startswith("ERROR"))
        self.assertIsNone(self.box.finished)
        self.box.call("finish", {"summary": "built it"})
        self.assertEqual(self.box.finished, "built it")


if __name__ == "__main__":
    unittest.main()
