import unittest

import db


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.create_schema(self.conn)
        self.album_id = db.upsert_album(
            self.conn,
            kind="thread",
            source_key="thread:313659",
            url="http://www.linkshop.com/club/archives/2006/313659.shtml",
            title="测试相册",
            section="商业设计",
            author="风兮",
            source_year=2006,
            posted_at="2006-11-08 15:25",
            page_count=3,
        )
        self.page1 = db.upsert_page(
            self.conn,
            album_id=self.album_id,
            page_no=1,
            url="http://www.linkshop.com/club/archives/2006/313659.shtml",
            title="测试相册",
        )
        self.page2 = db.upsert_page(
            self.conn,
            album_id=self.album_id,
            page_no=2,
            url="http://www.linkshop.com/club/archives/2006/313659_2.shtml",
            title="测试相册",
        )
        self.post1 = db.upsert_post(
            self.conn,
            album_id=self.album_id,
            page_id=self.page1,
            source_id="100",
            floor="楼主",
            author="风兮",
            posted_at="2006-11-08 15:25",
            body_text="正文",
            source_url="http://example/1#ssforum100",
        )
        self.post2 = db.upsert_post(
            self.conn,
            album_id=self.album_id,
            page_id=self.page2,
            source_id="101",
            floor="71楼",
            author="风兮",
            posted_at="2006-11-09 09:30",
            body_text="正文",
            source_url="http://example/2#ssforum101",
        )

    def tearDown(self):
        self.conn.close()

    def _insert(self, sha="a" * 64, caption="楼主 · 风兮", archived="2026-10-20T01:00:00+00:00"):
        return db.insert_image(
            self.conn,
            sha256=sha,
            rel_path=f"images/aa/{sha}.jpg",
            thumb_path=f"thumbs/{sha}.jpg",
            ext="jpg",
            mime="image/jpeg",
            width=800,
            height=1200,
            bytes_=12345,
            page_title="测试相册",
            caption=caption,
            alt_text=None,
            posted_at="2006-11-08 15:25",
            archived_at=archived,
        )

    def test_hash_dedup_and_sources(self):
        image_id, is_new = self._insert()
        same_id, second_new = self._insert(caption="71楼 · 风兮", archived="2026-10-21T01:00:00+00:00")
        self.assertTrue(is_new)
        self.assertFalse(second_new)
        self.assertEqual(image_id, same_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)

        self.assertTrue(
            db.add_image_source(
                self.conn,
                image_id=image_id,
                album_id=self.album_id,
                page_id=self.page1,
                post_id=self.post1,
                source_url="http://example/one.jpg",
                link_url=None,
                position=0,
                page_no=1,
                page_title="测试相册",
                posted_at="2006-11-08 15:25",
                archived_at="2026-10-20T01:00:00+00:00",
            )
        )
        self.assertTrue(
            db.add_image_source(
                self.conn,
                image_id=image_id,
                album_id=self.album_id,
                page_id=self.page2,
                post_id=self.post2,
                source_url="http://example/two.jpg",
                link_url=None,
                position=0,
                page_no=2,
                page_title="测试相册",
                posted_at="2006-11-09 09:30",
                archived_at="2026-10-21T01:00:00+00:00",
            )
        )
        # 同一图片 + 同一楼层 + 同一 URL 不应重复记录来源。
        self.assertFalse(
            db.add_image_source(
                self.conn,
                image_id=image_id,
                album_id=self.album_id,
                page_id=self.page1,
                post_id=self.post1,
                source_url="http://example/one.jpg",
                link_url=None,
                position=0,
                page_no=1,
                page_title="测试相册",
                posted_at="2006-11-08 15:25",
                archived_at="2026-10-20T01:00:00+00:00",
            )
        )

        stats = db.get_stats(self.conn)
        self.assertEqual(stats["albums"], 1)
        self.assertEqual(stats["images"], 1)
        self.assertEqual(stats["sources"], 2)
        self.assertEqual(stats["duplicates"], 1)

        rows = db.list_album_images(self.conn, self.album_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_title"], "测试相册")
        self.assertEqual(rows[0]["floor"], "楼主")
        self.assertEqual(rows[0]["source_count"], 2)

        image = db.get_image(self.conn, image_id)
        self.assertEqual(image["source_count"], 2)
        self.assertEqual(image["source_archived_at"], "2026-10-20T01:00:00+00:00")
        self.assertEqual(db.album_image_position(self.conn, self.album_id, image_id), 1)

    def test_search_and_listing(self):
        image_id, _ = self._insert()
        db.add_image_source(
            self.conn,
            image_id=image_id,
            album_id=self.album_id,
            page_id=self.page1,
            post_id=self.post1,
            source_url="http://example/one.jpg",
            link_url=None,
            position=0,
            page_no=1,
            page_title="测试相册",
            posted_at="2006-11-08 15:25",
            archived_at="2026-10-20T01:00:00+00:00",
        )
        albums = db.list_albums(self.conn)
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["image_count"], 1)
        self.assertEqual(db.search_images(self.conn, "测试")[0]["album_id"], self.album_id)
        self.assertEqual(db.search_albums(self.conn, "商业设计")[0]["id"], self.album_id)
        self.assertEqual(db.search_images(self.conn, "不存在"), [])

    def test_queue_lifecycle(self):
        self.assertTrue(
            db.enqueue_url(
                self.conn,
                url="http://www.linkshop.com/news/a.shtml",
                depth=1,
                parent_url="http://www.linkshop.com/",
            )
        )
        self.assertFalse(
            db.enqueue_url(
                self.conn,
                url="http://www.linkshop.com/news/a.shtml",
                depth=2,
            )
        )
        item = db.claim_next_url(self.conn)
        self.assertEqual(item["status"], "in_progress")
        db.finish_url(self.conn, url=item["url"], status="done", title="A")
        self.conn.commit()
        stats = db.queue_stats(self.conn)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(stats["pending"], 0)


if __name__ == "__main__":
    unittest.main()
