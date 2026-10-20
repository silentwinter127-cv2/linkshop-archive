import unittest

import parser


THREAD_HTML = """
<html><head><title>测试帖_商业设计_联商社区</title></head>
<body>
  <div id="posttop">阅读:[10] &nbsp; 跟帖:[1]</div>
  <div id="topictitle"><h2>主题：测试帖</h2></div>

  <a name="ssforum100"></a>
  <table><tr>
    <td><div class="main_leftuser"><a class="org">作者甲</a></div></td>
    <td>
      <div class="main_centop"><div class="l">发表于 2006-11-08 15:25</div><div class="r"><b>楼主</b></div></div>
      <table><tr><td class="main_text">
        <img src="/club/uploadfiles/face/a.gif">
        <img src="/club/uploadFiles/2006-11/one.jpg">
        <div class="textsign"><img src="/club/uploadFiles/2006-11/sign.jpg"></div>
      </td></tr></table>
    </td>
  </tr></table>

  <a name="ssforum101"></a>
  <table><tr>
    <td><div class="main_leftuser"><a class="org">作者乙</a></div></td>
    <td>
      <div class="main_centop"><div class="l">发表于 2006-11-08 15:26</div><div class="r"><b>2楼</b></div></div>
      <table><tr><td class="main_text">只有文字</td></tr></table>
    </td>
  </tr></table>
</body></html>
"""

LEGACY_THREAD_HTML = """
<html><head><title>旧帖子_零售图库_联商论坛</title></head>
<body>
  <h1 class="ltzd_jiaj">主题：旧帖子</h1>
  <a name="ssforum200"></a>
  <div class="ltzd_body">
    <div class="ltzd_L fL"><a href="/club/userinfo.aspx?uid=1"><h2>旧作者</h2></a></div>
    <div class="ltzd_R fL">
      <div class="ltzd_R_t">
        <span class="fL"><label>发表于 2008-07-03 14:52</label></span>
        <span class="fR">楼主</span>
      </div>
      <div class="ltzd_R_m">
        <p>旧模板正文</p>
        <img data-original="/club/uploadFiles/2008-7/two.jpg" src="/club/images/pic_loading.gif">
      </div>
    </div>
  </div>
</body></html>
"""

ARTICLE_HTML = """
<html><head>
  <title>文章标题_联商网</title>
  <meta property="og:title" content="文章标题">
  <meta property="article:published_time" content="2026-10-19 18:37">
  <meta name="author" content="联商网">
</head>
<body>
  <nav><ul class="breadcrumb">
    <li>联商首页</li><li>资讯动态</li><li>超市</li><li>正文</li>
  </ul></nav>
  <article class="page">
    <header>
      <h1>文章标题</h1>
      <div class="info"><span class="author">联商网 李娜</span><span class="time">2026-10-19 18:37</span></div>
    </header>
    <section>
      <p>第一段正文，用来确认容器识别正确。</p>
      <p><img src="/upload/article/a.jpg"></p>
      <p><img src="/upload/adver/ad.jpg"></p>
      <p><img src="http://blog.linkshop.com.cn/u/a/image/b.jpeg"></p>
    </section>
  </article>
</body></html>
"""


class ThreadParserTest(unittest.TestCase):
    def test_parse_classic_thread(self):
        page = parser.parse_thread_page(
            THREAD_HTML, "http://www.linkshop.com/club/archives/2006/313659.shtml"
        )
        self.assertEqual(page.title, "测试帖")
        self.assertEqual(page.board, "商业设计")
        self.assertEqual(page.reply_count, 1)
        self.assertEqual(len(page.posts), 2)
        self.assertEqual(page.posts[0].floor, "楼主")
        self.assertEqual(page.posts[0].author, "作者甲")
        self.assertEqual(page.posts[0].posted_at, "2006-11-08 15:25")
        self.assertEqual([image.url for image in page.posts[0].images],
                         ["http://www.linkshop.com/club/uploadFiles/2006-11/one.jpg"])

    def test_parse_legacy_lazy_thread(self):
        page = parser.parse_thread_page(
            LEGACY_THREAD_HTML, "http://www.linkshop.com/club/archives/2008/328594.shtml"
        )
        self.assertEqual(page.title, "旧帖子")
        self.assertEqual(page.board, "零售图库")
        self.assertEqual(len(page.posts), 1)
        self.assertEqual(page.posts[0].author, "旧作者")
        self.assertEqual(page.posts[0].images[0].url,
                         "http://www.linkshop.com/club/uploadFiles/2008-7/two.jpg")


class ArticleParserTest(unittest.TestCase):
    def test_parse_article(self):
        page = parser.parse_article_page(
            ARTICLE_HTML, "http://www.linkshop.com/news/2026553038.shtml"
        )
        self.assertEqual(page.title, "文章标题")
        self.assertEqual(page.section, "超市")
        self.assertEqual(page.author, "联商网 李娜")
        self.assertEqual(page.posted_at, "2026-10-19 18:37")
        self.assertEqual(len(page.images), 2)
        self.assertIn("/upload/article/a.jpg", page.images[0].url)
        self.assertIn("blog.linkshop.com.cn", page.images[1].url)


class UrlHelpersTest(unittest.TestCase):
    def test_thread_url(self):
        ref = parser.parse_thread_url(
            "http://www.linkshop.com/club/archives/2006/313659_3.shtml"
        )
        self.assertEqual(ref.source_id, "313659")
        self.assertEqual(ref.year, 2006)
        self.assertEqual(ref.page_no, 3)

    def test_build_page_url(self):
        base = "http://www.linkshop.com/club/archives/2006/313659_3.shtml"
        self.assertEqual(
            parser.build_page_url(base, 1),
            "http://www.linkshop.com/club/archives/2006/313659.shtml",
        )
        self.assertEqual(
            parser.build_page_url(base, 4),
            "http://www.linkshop.com/club/archives/2006/313659_4.shtml",
        )

    def test_estimate_page_count(self):
        self.assertEqual(parser.estimate_page_count(35, 94), 3)
        self.assertEqual(parser.estimate_page_count(10, 0), 1)

    def test_extract_links_skips_non_http(self):
        links = parser.extract_links(
            '<a href="/news/a.shtml">a</a><a href="javascript:void(0)">b</a>'
            '<a href="/news/a.shtml">dup</a>',
            "http://www.linkshop.com/",
        )
        self.assertEqual([link.url for link in links], ["http://www.linkshop.com/news/a.shtml"])


if __name__ == "__main__":
    unittest.main()
