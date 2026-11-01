import os
import tempfile
import unittest
from pathlib import Path

from mission_control.plan import Plan, Task
from mission_control.workspace import (
    PathEscapeError, append_decision, init_workspace, load_plan, resolve_inside, save_plan,
)


class PathJail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_paths_resolve_inside(self):
        p = resolve_inside(self.root, "backend/app.py")
        self.assertEqual(p, self.root.resolve() / "backend" / "app.py")

    def test_escape_attempts(self):
        for bad in ("../x", "a/../../x", "/etc/passwd", "C:/Windows", "", "   ", "..\\x"):
            with self.assertRaises(PathEscapeError, msg=bad):
                resolve_inside(self.root, bad)

    def test_dotdot_that_stays_inside_is_fine(self):
        self.assertEqual(resolve_inside(self.root, "a/../b.txt"), self.root.resolve() / "b.txt")

    def test_symlink_escape_is_blocked(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / "link")
        with self.assertRaises(PathEscapeError):
            resolve_inside(self.root, "link/secret.txt")

    def test_mission_dir_is_protected(self):
        with self.assertRaises(PathEscapeError):
            resolve_inside(self.root, ".mission/plan.json")
        self.assertTrue(resolve_inside(self.root, ".mission/plan.json", allow_mission=True))


class WorkspaceSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_creates_layout_and_copies_inputs_without_overwriting(self):
        src = self.base / "menu.txt"
        src.write_text("v1", encoding="utf-8")
        ws = init_workspace(self.base / "site", [src, src])
        self.assertTrue(ws.mission_dir.is_dir())
        self.assertEqual((ws.inputs_dir / "menu.txt").read_text(), "v1")
        self.assertEqual((ws.inputs_dir / "menu_2.txt").read_text(), "v1")
        self.assertEqual(src.read_text(), "v1")  # original untouched

    def test_init_is_idempotent_and_keeps_decisions(self):
        ws = init_workspace(self.base / "site")
        append_decision(ws, "d1", "use Flask", "orchestrator")
        ws2 = init_workspace(self.base / "site")
        self.assertIn("use Flask", ws2.decisions_path.read_text(encoding="utf-8"))

    def test_missing_input_file(self):
        with self.assertRaises(FileNotFoundError):
            init_workspace(self.base / "site", [self.base / "nope.pdf"])

    def test_plan_roundtrip(self):
        ws = init_workspace(self.base / "site")
        p = Plan(3, (Task("t1", "عنوان", "dev", "هدف", acceptance=("ok",)),))
        save_plan(ws, p)
        self.assertEqual(load_plan(ws), p)


if __name__ == "__main__":
    unittest.main()
