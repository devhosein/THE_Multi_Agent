import os
import unittest

from mission_control.paths import clean_path_input, looks_absolute


class CleanPath(unittest.TestCase):
    def test_windows_copy_as_path_quotes(self):
        # Explorer's "Copy as path" wraps the path in double quotes - the most likely reason a pasted path was refused
        self.assertEqual(clean_path_input(r'"C:\D Drive\Programing\THE_Multi_Agent\v1"'), r"C:\D Drive\Programing\THE_Multi_Agent\v1")

    def test_other_quote_styles_and_nesting(self):
        self.assertEqual(clean_path_input("'C:\\x'"), "C:\\x")
        self.assertEqual(clean_path_input("“C:\\x y”"), "C:\\x y")
        self.assertEqual(clean_path_input('""C:\\x""'), "C:\\x")
        self.assertEqual(clean_path_input('  "  C:\\x  "  '), "C:\\x")

    def test_unbalanced_quotes_are_left_alone(self):
        self.assertEqual(clean_path_input('"C:\\x'), '"C:\\x')

    def test_trailing_separators(self):
        self.assertEqual(clean_path_input("C:\\Users\\me\\"), "C:\\Users\\me")
        self.assertEqual(clean_path_input("C:/Users/me/"), "C:/Users/me")
        self.assertEqual(clean_path_input("  /home/me/  "), "/home/me")
        self.assertEqual(clean_path_input("\\\\server\\share\\dir\\"), "\\\\server\\share\\dir")

    def test_roots_are_preserved(self):
        for root in ("C:\\", "C:/", "C:", "/"):
            self.assertEqual(clean_path_input(root), root)

    def test_file_urls(self):
        self.assertEqual(clean_path_input("file:///C:/Users/me/My%20Folder"), "C:/Users/me/My Folder")
        self.assertEqual(clean_path_input("file:///home/me/a%20b"), "/home/me/a b")
        self.assertEqual(clean_path_input("file://localhost/home/me"), "/home/me")

    def test_environment_variables_and_home(self):
        os.environ["MC_TEST_DIR"] = "/opt/mc"
        try:
            self.assertEqual(clean_path_input("%MC_TEST_DIR%\\code"), "/opt/mc\\code")
            self.assertEqual(clean_path_input("$MC_TEST_DIR/code"), "/opt/mc/code")
        finally:
            del os.environ["MC_TEST_DIR"]
        self.assertEqual(clean_path_input("%MC_SURELY_UNSET_VAR%\\x"), "%MC_SURELY_UNSET_VAR%\\x")   # unknown: left as typed
        self.assertEqual(clean_path_input("~"), os.path.expanduser("~"))

    def test_dollar_in_a_folder_name_survives(self):
        self.assertEqual(clean_path_input("C:\\shares\\money$\\x"), "C:\\shares\\money$\\x")

    def test_empty_and_none(self):
        self.assertEqual(clean_path_input(""), "")
        self.assertEqual(clean_path_input(None), "")
        self.assertEqual(clean_path_input('  ""  '), "")

    def test_non_latin_paths_are_untouched(self):
        self.assertEqual(clean_path_input('"C:\\پروژه‌ها\\کافه"'), "C:\\پروژه‌ها\\کافه")


class LooksAbsolute(unittest.TestCase):
    def test_absolute_on_any_platform(self):
        for p in ("C:\\x", "C:/x", "d:\\y\\z", "/home/me", "\\\\server\\share"):
            self.assertTrue(looks_absolute(p), p)

    def test_relative(self):
        for p in ("", "x/y", "./x", "..\\x", "C:x", "my folder"):
            self.assertFalse(looks_absolute(p), p)


if __name__ == "__main__":
    unittest.main()
