import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import crawler


class CrawlerUrlTest(unittest.TestCase):
    def test_canonicalize(self):
        self.assertEqual(
            crawler.canonicalize_url(
                "https://www.linkshop.com/news/a.shtml?utm_source=x&id=2#top"
            ),
            "http://www.linkshop.com/news/a.shtml?id=2",
        )
        self.assertEqual(
            crawler.canonicalize_url("http://linkshop.com/club/"),
            "http://www.linkshop.com/club/",
        )
        self.assertEqual(
            crawler.canonicalize_url("http://www.linkshop.com.cn/news/a.shtml"),
            "http://www.linkshop.com/news/a.shtml",
        )
        self.assertIsNone(crawler.canonicalize_url("http://www.soupu.com/"))

    def test_blocked_and_scope(self):
        self.assertTrue(crawler.is_blocked("http://www.linkshop.com/club/uploadFiles/a.jpg"))
        self.assertTrue(crawler.is_blocked("http://www.linkshop.com/club/reannounce.aspx?id=1"))
        self.assertTrue(crawler.is_blocked("http://www.linkshop.com/news/a.pdf"))
        self.assertFalse(crawler.is_blocked("http://www.linkshop.com/news/a.shtml"))
        self.assertTrue(crawler.in_scope("http://www.linkshop.com/club/", "forum"))
        self.assertFalse(crawler.in_scope("http://www.linkshop.com/news/", "forum"))
        self.assertTrue(crawler.in_scope("http://www.linkshop.com/news/", "news"))

    def test_probe_image(self):
        buffer = io.BytesIO()
        Image.new("RGB", (320, 240), (180, 60, 40)).save(buffer, "PNG")
        probe = crawler.probe_image(buffer.getvalue())
        self.assertEqual((probe.width, probe.height), (320, 240))
        self.assertEqual(probe.ext, "png")
        self.assertEqual(probe.mime, "image/png")

    def test_board_helpers(self):
        self.assertTrue(
            crawler.is_board_list_url(
                "http://www.linkshop.com/club/list.aspx?boardid=20&filter=&gopage=2"
            )
        )
        self.assertEqual(crawler.board_page_number("http://x/list.aspx?boardid=20&gopage=250"), 250)
        self.assertEqual(
            crawler.board_page_url(
                "http://www.linkshop.com/club/list.aspx?boardid=20&filter=&gopage=1", 198
            ),
            "http://www.linkshop.com/club/list.aspx?boardid=20&filter=&gopage=198",
        )
        html = """
        <div class="page_selec"><input name="gopage" value="1" /> /250</div>
        <a href="archives/2006/291744.shtml">老帖</a>
        """
        self.assertEqual(crawler.SiteCrawler.board_total_pages(html), 250)
        self.assertTrue(crawler.SiteCrawler.board_has_threads(html))
        self.assertFalse(
            crawler.SiteCrawler.board_has_threads(
                '<a href="http://www.linkshop.com/club/archives/2017/788978.shtml">法律声明</a>'
            )
        )


class PriorityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.crawler = crawler.SiteCrawler(
            data_dir=Path(self.temp.name),
            scope="site",
            order="oldest",
            delay=0.2,
            timeout=5,
            retries=0,
            max_pages=1,
            max_depth=5,
            max_images=0,
            min_width=1,
            min_height=1,
            min_bytes=1,
        )

    def tearDown(self):
        self.crawler.conn.close()
        self.temp.cleanup()

    def test_oldest_threads_first(self):
        old = self.crawler.priority_for_url(
            "http://www.linkshop.com/club/archives/2006/291744.shtml"
        )
        new = self.crawler.priority_for_url(
            "http://www.linkshop.com/club/archives/2026/1164769.shtml"
        )
        article = self.crawler.priority_for_url(
            "http://www.linkshop.com/news/2026553038.shtml"
        )
        self.assertLess(old, new)
        self.assertLess(new, article)
        self.assertEqual(
            self.crawler.priority_for_url("http://www.linkshop.com/club/list.aspx?boardid=20"),
            700,
        )


if __name__ == "__main__":
    unittest.main()
