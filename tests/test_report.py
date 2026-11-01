import tempfile
import unittest
from pathlib import Path

from mission_control.mock import build_mock_run
from mission_control.report import build_report, environment


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.text = build_report(build_mock_run(Path(cls.tmp.name) / "m.jsonl"), environment({"ollama host": "http://x"}))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sections(self):
        for heading in ("## Environment", "## Run", "## Agents", "## Tasks", "## Plan changes", "## Errors and warnings",
                        "## Decision ledger", "## Last log lines"):
            self.assertIn(heading, self.text)

    def test_contents(self):
        self.assertIn("- **mode**: demo", self.text)
        self.assertIn("**ollama host**: http://x", self.text)
        self.assertIn("running → failed → running → done → verified", self.text)     # t5's story
        self.assertIn("test_reservation_validation", self.text)                       # evidence from the log detail
        self.assertIn("| a_frontend | frontend | ornith-1.5:9b |", self.text)

    def test_a_running_run_and_an_empty_log(self):
        evs = build_mock_run(Path(self.tmp.name) / "m2.jsonl")[:40]
        self.assertIn("still running", build_report(evs))
        self.assertIn("None.", build_report([]))

    def test_table_cells_cannot_break_the_table(self):
        from mission_control.report import _cell
        self.assertEqual(_cell("a|b\nc"), "a\\|b c")
