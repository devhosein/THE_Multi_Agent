"""The command-line scripts, run the way a user runs them."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Scripts(unittest.TestCase):
    def test_replay_prints_to_a_pipe_on_a_legacy_code_page(self):
        """Windows gives redirected output the cp1252 code page, which cannot encode the text view's symbols or
        Persian. Forcing that code page here reproduces it on every OS; the script must not crash."""
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["PYTHONIOENCODING"] = "cp1252"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "replay.py"), str(ROOT / "sample" / "mock_run.jsonl"),
                            "--speed", "0", "--no-chunks"], capture_output=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-600:])
        out = r.stdout.decode("utf-8")
        self.assertIn("■", out)
        self.assertGreater(len(out.splitlines()), 20)


if __name__ == "__main__":
    unittest.main()
