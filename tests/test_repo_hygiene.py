"""Repository-wide hygiene: every Python file compiles with warnings treated as errors.
(An unescaped backslash like "C:\\Projects" in a docstring is only a warning today and a syntax error tomorrow.)"""
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Hygiene(unittest.TestCase):
    def test_all_python_files_compile_without_warnings(self):
        files = [p for p in ROOT.rglob("*.py") if "e2e-shots" not in p.parts and "__pycache__" not in p.parts]
        self.assertGreater(len(files), 20)
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                with warnings.catch_warnings():
                    warnings.simplefilter("error")
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
