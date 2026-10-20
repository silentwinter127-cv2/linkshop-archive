"""联商网页面解析器。

两类核心页面：
* 论坛帖子：/club/archives/YYYY/<id>[_页码].shtml，解析楼层与正文图片；
* 资讯/专栏等文章：通用正文容器识别，解析文章标题、栏目、发布时间与正文图片。

解析器不访问网络、不写数据库，方便单元测试。
"""
from __future__ import annotations

import html as html_lib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass(slots=True)
class ParsedImage:
    url: str
    link_url: str | None
    alt: str | None
    position: int


@dataclass(slots=True)
class ParsedPost:
    source_id: str
    floor: str
    author: str
    posted_at: str
    body_text: str
    images: list[ParsedImage] = field(default_factory=list)


@dataclass(slots=True)
class ParsedThreadPage:
    title: str
    raw_title: str
    board: str
    author: str
    posted_at: str
    reply_count: int
    page_no: int
    posts: list[ParsedPost] = field(default_factory=list)


@dataclass(slots=True)
class ParsedArticlePage:
    title: str
    raw_title: str
    section: str
    author: str
    posted_at: str
    body_text: str
    images: list[ParsedImage] = field(default_factory=list)


@dataclass(slots=True)
class ThreadRef:
    source_id: str
    year: int | None
    page_no: int


@dataclass(slots=True)
class Link:
    url: str
    text: str
    nofollow: bool = False


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def decode_html(content: bytes, content_type: str | None = None) -> str:
    """按页面声明解码；这个老站声明 gb2312，实际常用 gb18030 超集。"""
    if content.startswith(b"\xef\xbb\xbf"):
        return content.decode("utf-8-sig", errors="replace")

    charset = None
    if content_type:
        match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
        if match:
            charset = match.group(1)
    if not charset:
        match = re.search(
            rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w-]+)", content[:8192], re.I
        )
        if match:
            charset = match.group(1).decode("ascii", errors="ignore")

    aliases = {
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "x-gbk": "gb18030",
        "gb_2312-80": "gb18030",
        "utf8": "utf-8",
    }
    candidates: list[str] = []
    if charset:
        candidates.append(aliases.get(charset.lower(), charset))
    candidates.extend(["gb18030", "utf-8"])

    for encoding in dict.fromkeys(candidates):
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("utf-8", errors="replace")


def normalize_url(base_url: str, raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    raw_url = html_lib.unescape(raw_url).strip().strip("\"'")
    if not raw_url or raw_url.lower().startswith(("data:", "javascript:", "mailto:", "tel:")):
        return None
    absolute = urljoin(base_url, raw_url)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def is_image_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return Path(path).suffix in IMAGE_EXTENSIONS


IMAGE_HOST_SUFFIXES = (".linkshop.com", ".linkshop.com.cn", ".linkshop.cn")


def _image_host_ok(url: str) -> bool:
    host = (urlsplit(url).netloc or "").lower().split(":")[0]
    return host == "linkshop.com" or host == "linkshop.com.cn" or host.endswith(IMAGE_HOST_SUFFIXES)


def is_forum_image(url: str) -> bool:
    """论坛正文图片：站内上传目录或联商图片站，排除头像/表情/图标。"""
    path = urlsplit(url).path.lower()
    blocked = ("/face/", "/images/", "/pic/", "/img/", "avatar", "logo", "icon", "emot", "smilies")
    if any(token in path for token in blocked):
        return False
    if not _image_host_ok(url):
        return False
    allowed = ("/uploadfiles/", "/upload/", "/atlas/")
    if not any(token in path for token in allowed):
        # 早期帖子把图片放在 blog.linkshop.com.cn/u/<user>/image/ 下。
        if not ("/u/" in path and "/image/" in path):
            return False
    suffix = Path(path).suffix
    return suffix in IMAGE_EXTENSIONS or suffix == ""


def is_article_image(url: str) -> bool:
    """文章正文图片：只接受文章/图集上传目录，排除广告与站点装饰。"""
    if not _image_host_ok(url):
        return False
    path = urlsplit(url).path.lower()
    blocked = (
        "/upload/adver/",
        "/upload/adv/",
        "/2017/images/",
        "/images/",
        "/pic/",
        "/face/",
        "avatar",
        "logo",
        "icon",
        "banner",
        "qrcode",
        "weixin",
        "wxsp",
    )
    if any(token in path for token in blocked):
        return False
    allowed = ("/upload/article/", "/uploadfiles/", "/upload/", "/atlas/")
    if not any(token in path for token in allowed):
        if not ("/u/" in path and "/image/" in path):
            return False
    suffix = Path(path).suffix
    return suffix in IMAGE_EXTENSIONS or suffix == ""


def parse_thread_url(url: str) -> ThreadRef:
    match = re.search(
        r"/archives/(?P<year>\d{4})/(?P<id>\d+)(?:_(?P<page>\d+))?\.shtml",
        url,
        re.I,
    )
    if not match:
        raise ValueError(
            "暂只支持 /club/archives/YYYY/<帖子ID>[_页码].shtml 形式的联商社区帖子"
        )
    return ThreadRef(
        source_id=match.group("id"),
        year=int(match.group("year")),
        page_no=int(match.group("page") or 1),
    )


def build_page_url(url: str, page_no: int) -> str:
    match = re.match(
        r"^(?P<base>https?://[^/]+/.*?/archives/\d{4}/\d+)(?:_\d+)?\.shtml(?P<query>\?.*)?$",
        url,
        re.I,
    )
    if not match:
        raise ValueError(f"无法从 URL 推导分页地址: {url}")
    suffix = "" if page_no == 1 else f"_{page_no}"
    return f"{match.group('base')}{suffix}.shtml{match.group('query') or ''}"


def estimate_page_count(post_count_on_first_page: int, reply_count: int) -> int:
    total_posts = max(1, reply_count + 1)
    return max(1, math.ceil(total_posts / max(1, post_count_on_first_page)))


def _parse_title(soup: BeautifulSoup) -> str:
    raw = _clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    return raw


def _strip_site_suffix(title: str) -> str:
    title = _clean(title)
    for suffix in ("_联商社区", "_联商网", "-联商网", "—联商网", "| 联商网"):
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return re.split(r"[_|｜]", title)[0].strip()


def _breadcrumb_section(soup: BeautifulSoup) -> str:
    crumbs = [
        _clean(item.get_text(" ", strip=True))
        for item in soup.select(".breadcrumb li, .crumb li, .position li")
    ]
    crumbs = [
        item
        for item in crumbs
        if item and item not in {"首页", "联商首页", "联商网首页", "正文", ">", "»"}
    ]
    return crumbs[-1] if crumbs else ""


def _find_date(text: str) -> str:
    match = re.search(r"(20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}(?:[ T]\d{1,2}:\d{2})?)", text)
    return match.group(1).replace("年", "-").replace("月", "-").replace("日", "") if match else ""


def _thread_images(content, page_url: str) -> list[ParsedImage]:
    # 签名档里也会有图片、链接，先移除，避免污染正文与图片集合。
    for removable in content.select(".textsign, script, style"):
        removable.decompose()

    images: list[ParsedImage] = []
    for img in content.find_all("img"):
        # 老模板用 data-original 做懒加载，src 只是占位图，必须优先取真实地址。
        src = img.get("data-original") or img.get("data-src") or img.get("src")
        image_url = normalize_url(page_url, src)
        if not image_url or not is_forum_image(image_url):
            continue

        link_url = None
        parent_link = img.find_parent("a")
        if parent_link:
            candidate = normalize_url(page_url, parent_link.get("href"))
            if candidate and is_image_url(candidate) and candidate != image_url:
                link_url = candidate

        images.append(
            ParsedImage(
                url=image_url,
                link_url=link_url,
                alt=_clean(img.get("alt")) or None,
                position=len(images),
            )
        )
    return images


def _parse_thread_page(soup: BeautifulSoup, html: str, page_url: str) -> ParsedThreadPage:
    raw_title = _parse_title(soup)
    title = ""
    heading = soup.select_one("#topictitle h2, h1.ltzd_jiaj")
    if heading:
        title = re.sub(r"^\s*主题\s*[:：]\s*", "", _clean(heading.get_text(" ", strip=True)))
    if not title and raw_title:
        title = re.split(r"[_|｜]", raw_title)[0].strip()

    parts = [part.strip() for part in re.split(r"[_|｜]", raw_title) if part.strip()]
    board = parts[1] if len(parts) >= 2 else ""

    posttop = soup.select_one("#posttop, .ltzd_top")
    reply_count = 0
    if posttop:
        match = re.search(
            r"跟帖\s*[:：]\s*\[\s*(\d+)\s*\]", posttop.get_text(" ", strip=True)
        )
        if match:
            reply_count = int(match.group(1))

    page_no = 1
    match = re.search(r"ShowPagen?\s*\(\s*[^,]+,\s*(\d+)", html)
    if match:
        page_no = int(match.group(1))
    else:
        url_match = re.search(r"/archives/\d{4}/\d+_(\d+)\.shtml", page_url)
        if url_match:
            page_no = int(url_match.group(1))

    posts: list[ParsedPost] = []
    anchors = soup.find_all("a", attrs={"name": re.compile(r"^ssforum\d+$")})
    uses_ltzd = bool(soup.select_one("div.ltzd_body"))

    for anchor in anchors:
        source_id = str(anchor.get("name", ""))[len("ssforum") :]

        if uses_ltzd:
            # 新版论坛模板：div.ltzd_body > .ltzd_L / .ltzd_R > .ltzd_R_m
            body = anchor.find_next("div", class_=re.compile(r"\bltzd_body\b"))
            if body is None:
                continue
            content = body.select_one(".ltzd_R_m")
            if content is None:
                continue
            author_el = body.select_one(".ltzd_L h2")
            author = _clean(author_el.get_text(" ", strip=True)) if author_el else ""
            top = body.select_one(".ltzd_R_t")
            date_el = top.select_one("label") if top else None
            posted_at = _clean(date_el.get_text(" ", strip=True)) if date_el else ""
            posted_at = re.sub(r"^发表于\s*", "", posted_at)
            floor_el = top.select_one(".fR") if top else None
            floor = _clean(floor_el.get_text(" ", strip=True)) if floor_el else ""
        else:
            table = anchor.find_next("table")
            if table is None or table.select_one("td.main_text") is None:
                continue

            author_el = table.select_one(".main_leftuser a.org")
            author = _clean(author_el.get_text(" ", strip=True)) if author_el else ""

            date_el = table.select_one(".main_centop .l")
            posted_at = _clean(date_el.get_text(" ", strip=True)) if date_el else ""
            posted_at = re.sub(r"^发表于\s*", "", posted_at)

            floor_el = table.select_one(".main_centop .r b")
            floor = _clean(floor_el.get_text(" ", strip=True)) if floor_el else ""

            content = table.select_one("td.main_text")
            if content is None:
                continue

        body_text = _clean(content.get_text(" ", strip=True))[:6000]
        posts.append(
            ParsedPost(
                source_id=source_id,
                floor=floor or "—",
                author=author,
                posted_at=posted_at,
                body_text=body_text,
                images=_thread_images(content, page_url),
            )
        )

    if not title:
        title = f"帖子 {parse_thread_url(page_url).source_id}"
    first = posts[0] if posts else None
    return ParsedThreadPage(
        title=title,
        raw_title=raw_title,
        board=board,
        author=first.author if first else "",
        posted_at=first.posted_at if first else "",
        reply_count=reply_count,
        page_no=page_no,
        posts=posts,
    )


def _article_container(soup: BeautifulSoup):
    candidates: list[tuple[int, object]] = []
    selectors = [
        "article.page",
        "article.article-content",
        "article.article_content",
        "article",
        ".article-content",
        ".article_content",
        ".news_content",
        ".detail-content",
        ".content_left article",
        "#content",
        ".main_text",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            p_count = len(node.select("p"))
            img_count = len(node.select("img"))
            has_heading = bool(node.select_one("h1, h2"))
            score = p_count * 3 + img_count * 2 + (8 if has_heading else 0)
            if score > 0:
                candidates.append((score, node))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def _extract_images(container, page_url: str, checker) -> list[ParsedImage]:
    images: list[ParsedImage] = []
    for img in container.find_all("img"):
        src = img.get("data-original") or img.get("data-src") or img.get("src")
        image_url = normalize_url(page_url, src)
        if not image_url or not checker(image_url):
            continue
        link_url = None
        parent_link = img.find_parent("a")
        if parent_link:
            candidate = normalize_url(page_url, parent_link.get("href"))
            if candidate and is_image_url(candidate) and candidate != image_url:
                link_url = candidate
        images.append(
            ParsedImage(
                url=image_url,
                link_url=link_url,
                alt=_clean(img.get("alt")) or None,
                position=len(images),
            )
        )
    return images


def _parse_article_page(soup: BeautifulSoup, page_url: str) -> ParsedArticlePage:
    raw_title = _parse_title(soup)
    og_title = soup.select_one('meta[property="og:title"]')
    title = _clean(og_title.get("content")) if og_title else ""
    container = _article_container(soup)
    if container is not None:
        heading = container.select_one("h1")
        if heading:
            title = _clean(heading.get_text(" ", strip=True))
    if not title:
        title = _strip_site_suffix(raw_title)
    if not title:
        title = page_url

    section = _breadcrumb_section(soup)
    if not section:
        path = urlsplit(page_url).path.strip("/").split("/")
        section = path[0] if path and path[0] else ""

    author = ""
    for selector in (".info .author", ".article-info .author", ".author", ".source"):
        node = soup.select_one(selector)
        if node:
            author = _clean(node.get_text(" ", strip=True))
            break
    if not author:
        meta_author = soup.select_one('meta[name="author"]')
        if meta_author:
            author = _clean(meta_author.get("content"))

    posted_at = ""
    meta_time = soup.select_one('meta[property="article:published_time"]')
    if meta_time:
        posted_at = _clean(meta_time.get("content"))
    if not posted_at:
        for selector in (".info .time", ".article-info .time", ".time", ".date"):
            node = soup.select_one(selector)
            if node:
                posted_at = _clean(node.get_text(" ", strip=True))
                break
    if not posted_at:
        posted_at = _find_date(soup.get_text(" ", strip=True)[:3000])

    body_text = ""
    images: list[ParsedImage] = []
    if container is not None:
        for removable in container.select("script, style, nav, footer, .share, .share_box"):
            removable.decompose()
        body_text = _clean(container.get_text(" ", strip=True))[:8000]
        images = _extract_images(container, page_url, is_article_image)

    return ParsedArticlePage(
        title=title,
        raw_title=raw_title,
        section=section,
        author=author,
        posted_at=posted_at,
        body_text=body_text,
        images=images,
    )


def is_thread_url(url: str) -> bool:
    return bool(re.search(r"/club/archives/\d{4}/\d+(?:_\d+)?\.shtml", url, re.I))


def is_article_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if is_thread_url(url) or path.endswith("/"):
        return False
    # 栏目首页 / 列表页不应被当成文章；详情页通常带 3 位以上数字 ID。
    if not re.search(r"\d{3,}", path):
        return False
    return any(
        token in path
        for token in ("/news/", "/column/", "/interview/", "/data/", "/pdl/", "/case/", "/cnma/", "/atlas/")
    )


def parse_article_page(html: str, page_url: str) -> ParsedArticlePage:
    soup = BeautifulSoup(html, "html.parser")
    return _parse_article_page(soup, page_url)


def parse_thread_page(html: str, page_url: str) -> ParsedThreadPage:
    soup = BeautifulSoup(html, "html.parser")
    return _parse_thread_page(soup, html, page_url)


def extract_links(html: str, page_url: str) -> list[Link]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[Link] = []
    for anchor in soup.find_all("a", href=True):
        url = normalize_url(page_url, anchor.get("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        rel = anchor.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        links.append(
            Link(
                url=url,
                text=_clean(anchor.get_text(" ", strip=True))[:120],
                nofollow="nofollow" in [item.lower() for item in rel],
            )
        )
    return links


def canonical_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return _parse_title(soup)
