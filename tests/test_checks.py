"""Mechanical checks after a worker says it is done."""
import tempfile
import unittest
from pathlib import Path

from mission_control.checks import check_task
from mission_control.plan import Task
from mission_control.workspace import init_workspace


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = init_workspace(Path(self.tmp.name) / "proj")
        self.task = Task("t1", "Contract", "architect", "Write the contract", owns=("contracts/**", "app.py"), acceptance=("ok",))

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, rel, text):
        p = self.ws.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
        return check_task(self.ws, self.task, [rel])

    def test_a_contract_must_say_how_the_parts_run_together(self):
        """Real run cafe-35b: every endpoint was described, but not who serves the pages."""
        problems = self.check("contracts/api.md", "# API\nGET /api/menu returns the menu\n")
        self.assertEqual(len(problems), 1)
        self.assertIn('"## Integration"', problems[0])
        self.assertIn("relative URLs", problems[0])

    def test_integration_heading_variants_are_accepted(self):
        for heading in ("## Integration", "### 4. Integration and deployment", "# INTEGRATION", "## 2) Integration"):
            with self.subTest(heading=heading):
                self.assertEqual(self.check("contracts/api.md", f"# API\n{heading}\nThe backend serves the pages.\n"), [])

    def test_the_word_in_a_sentence_is_not_a_section(self):
        self.assertTrue(self.check("contracts/api.md", "# API\nIntegration is easy: just call it.\n"))

    def test_only_contracts_need_it(self):
        self.assertEqual(self.check("app.py", "print('hi')\n"), [])

    def test_python_syntax_errors_are_reported_with_the_line(self):
        problems = self.check("app.py", "def f(:\n")
        self.assertIn("line 1", problems[0])


if __name__ == "__main__":
    unittest.main()
