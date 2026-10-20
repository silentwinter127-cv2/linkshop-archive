import unittest

from utils import format_date, format_datetime, human_size, safe_filename


class UtilsTest(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(safe_filename('a/b:c*?"'), "a_b_c___")
        self.assertEqual(safe_filename("  "), "image")
        self.assertEqual(safe_filename("标题", limit=2), "标题")

    def test_datetime(self):
        self.assertEqual(format_datetime("2006-11-08 15:25"), "2006-11-08 15:25")
        self.assertEqual(format_date("2006-11-08 15:25"), "2006-11-08")
        self.assertEqual(format_datetime(""), "—")
        self.assertRegex(format_datetime("2026-10-20T01:03:02+00:00"), r"^2026-10-20 \d{2}:03$")

    def test_filesize(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(3 * 1024 * 1024), "3.0 MB")


if __name__ == "__main__":
    unittest.main()
