"""联商网全站图片存档爬虫。

特点：
* 从首页 / 栏目页 / 任意种子页出发，BFS 发现全站链接；
* SQLite 持久化队列，中断后原样续爬；
* 自动识别论坛帖（整帖分页、楼层）与文章（正文、栏目、发布时间）；
* SHA-256 内容去重，重复图片只存一份，保留多个来源；
* 每张图片记录：存档时间、原帖/文章发布时间、EXIF 拍摄时间（有则记录）、HTTP Last-Modified；
* 遵守 robots.txt，默认 2 秒请求间隔。

示例：
    python crawler.py --scope site --max-pages 500
    python crawler.py "http://www.linkshop.com/club/archives/2006/313659_3.shtml"
    python crawler.py "http://www.linkshop.com/news/2026553038.shtml" --max-images 80
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import random
import re
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

warnings.filterwarnings("ignore", category=UserWarning, module=r"requests(\.|$)")

import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

import db
import parser
from utils import safe_filename

USER_AGENT = "LinkshopArchiveBot/1.0 (personal image archive; respectful crawler)"
BASE_HOST = "www.linkshop.com"
ALLOWED_HOSTS = {"www.linkshop.com", "linkshop.com", "www.linkshop.com.cn", "linkshop.com.cn"}
# 20 = 零售图库（老 DM / 海报 / 门店图最集中的版块），放在最前面。
BOARD_IDS = (20, 2, 4, 1, 12, 23)
FORUM_BOARD_URL = "http://www.linkshop.com/club/list.aspx?boardid={board_id}"
MAX_IMAGE_BYTES = 40 * 1024 * 1024
THUMB_SIZE = (720, 720)
THUMB_QUALITY = 82

BLOCKED_PATH_RE = re.compile(
    r"/(?:login|logout|register|reg\.shtml|findpass|cart|order|pay|admin|members|"
    r"search|userinfo|usersms|reannounce|manbbs|changeann|twiter|fav|countjs|"
    r"getipcityjs|adg|upload\.aspx|announce|downfile|attachment|group|"
    r"createSmallImage|Article_Count|get_linkshopAds|print)",
    re.I,
)
BLOCKED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".pdf", ".zip", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".mp3", ".mp4", ".avi", ".mov", ".css", ".js",
    ".xml", ".txt", ".ico", ".woff", ".woff2", ".ttf",
}

FORMAT_META = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
    "BMP": ("bmp", "image/bmp"),
    "TIFF": ("tif", "image/tiff"),
}


class FetchError(RuntimeError):
    pass


class NotFound(FetchError):
    pass


@dataclass(slots=True)
class ImageProbe:
    width: int
    height: int
    ext: str
    mime: str
    exif_taken_at: str


class RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.2, interval)
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        remaining = self.interval - elapsed
        if remaining > 0:
            time.sleep(remaining + random.uniform(0.0, 0.15))
        self._last = time.monotonic()


def canonicalize_url(url: str) -> str | None:
    """统一到 http://www.linkshop.com，去掉 fragment 与常见跟踪参数。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host in {"linkshop.com", "linkshop.com.cn", "www.linkshop.com.cn"}:
        host = BASE_HOST
    if host != BASE_HOST:
        return None
    path = parts.path or "/"
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in {"spm", "from", "share", "_t", "timestamp"}:
            continue
        query_items.append((key, value))
    query = urlencode(query_items, doseq=True)
    return urlunsplit(("http", BASE_HOST, path, query, ""))


def is_board_list_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path.lower().endswith("/club/list.aspx") and "boardid=" in parts.query.lower()


def board_page_number(url: str) -> int:
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.lower() in {"gopage", "page"}:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return 1
    return 1


def board_page_url(url: str, page_no: int) -> str:
    parts = urlsplit(url)
    items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"gopage", "page"}
    ]
    items.append(("gopage", str(page_no)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(items), ""))


def is_blocked(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path
    if Path(path).suffix.lower() in BLOCKED_EXTENSIONS:
        return True
    target = f"{path}?{parts.query}" if parts.query else path
    if BLOCKED_PATH_RE.search(target):
        return True
    if path.lower().startswith(("/club/uploadfiles/", "/upload/")):
        return True
    return False


def in_scope(url: str, scope: str) -> bool:
    path = urlsplit(url).path.lower()
    if scope == "forum":
        return path.startswith("/club/")
    if scope == "news":
        return path.startswith(
            ("/news/", "/column/", "/interview/", "/data/", "/pdl/", "/case/", "/cnma/", "/atlas/")
        )
    return True


def _exif_time(image: Image.Image) -> str:
    try:
        exif = image.getexif()
    except Exception:
        return ""
    for tag in (36867, 306):  # DateTimeOriginal, DateTime
        raw = exif.get(tag)
        if not raw:
            continue
        match = re.match(
            r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(raw)
        )
        if match:
            return "{}-{}-{} {}:{}:{}".format(*match.groups())
    return ""


def probe_image(data: bytes) -> ImageProbe:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        fmt = (image.format or "").upper()
        if fmt not in FORMAT_META:
            raise UnidentifiedImageError(f"unsupported image format: {fmt or 'unknown'}")
        ext, mime = FORMAT_META[fmt]
        return ImageProbe(
            width=image.width,
            height=image.height,
            ext=ext,
            mime=mime,
            exif_taken_at=_exif_time(image),
        )


def write_thumbnail(data: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        thumb = image.copy()
        thumb.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        if thumb.mode in ("RGBA", "LA") or (
            thumb.mode == "P" and "transparency" in thumb.info
        ):
            rgba = thumb.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (246, 241, 231))
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            thumb = canvas
        else:
            thumb = thumb.convert("RGB")
        thumb.save(target, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)


class SiteCrawler:
    def __init__(
        self,
        *,
        data_dir: Path,
        scope: str,
        order: str,
        delay: float,
        timeout: float,
        retries: int,
        max_pages: int,
        max_depth: int,
        max_images: int,
        min_width: int,
        min_height: int,
        min_bytes: int,
    ) -> None:
        self.data_dir = data_dir
        self.images_dir = data_dir / "images"
        self.thumbs_dir = data_dir / "thumbs"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = data_dir / "archive.db"
        self.conn = db.init_db(self.db_path)
        self.scope = scope
        self.order = order
        self.timeout = timeout
        self.retries = retries
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.max_images = max_images
        self.min_width = min_width
        self.min_height = min_height
        self.min_bytes = min_bytes

        self.limiter = RateLimiter(delay)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*;q=0.8,*/*;q=0.5",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            }
        )
        self.robots: RobotFileParser | None = None
        self.robots_delay = 0.0
        self.album_ids: set[int] = set()
        self.url_cache: dict[str, int] = {}
        self.images_seen = 0
        self.images_new = 0
        self.images_duplicate = 0
        self.images_downloaded = 0
        self.images_skipped = 0
        self.image_errors = 0
        self.errors = 0
        self.pages_fetched = 0
        self.stop_reason = ""

    # ------------------------------------------------------------------ setup
    def load_robots(self) -> None:
        parser_ = RobotFileParser()
        parser_.set_url(f"http://{BASE_HOST}/robots.txt")
        try:
            parser_.read()
            self.robots = parser_
            delay = parser_.crawl_delay(USER_AGENT) or parser_.crawl_delay("*")
            if delay:
                self.robots_delay = float(delay)
        except Exception as exc:
            self.log(f"robots.txt 读取失败，按默认规则继续：{exc}", "warning")

    @staticmethod
    def log(message: str, level: str = "info") -> None:
        icon = {"info": "·", "ok": "✓", "warn": "!", "error": "×"}.get(level, "·")
        print(f"{icon} {message}", flush=True)

    def allowed(self, url: str, depth: int) -> bool:
        if depth > self.max_depth:
            return False
        if not in_scope(url, self.scope):
            return False
        if is_blocked(url):
            return False
        if self.robots is not None:
            try:
                if not self.robots.can_fetch(USER_AGENT, url):
                    return False
            except Exception:
                pass
        return True

    def priority_for_url(self, url: str) -> int:
        """最老优先：年份 / 版块页码越小，priority 数字越小，越先抓。"""
        if self.order != "oldest":
            return 100
        if url in {"http://www.linkshop.com/", "http://www.linkshop.com"}:
            return 10
        path = urlsplit(url).path.lower()
        if path in {"/club/", "/club"}:
            return 20
        if path in {"/news/", "/news"}:
            return 40
        if is_board_list_url(url):
            page_no = board_page_number(url)
            # 只有显式种子的 5 个主版块第 1 页会排在 30；
            # 从论坛首页顺带发现的其它版块放到老帖之后，避免抢占。
            return 700 if page_no == 1 else 900 + page_no
        if parser.is_thread_url(url):
            try:
                ref = parser.parse_thread_url(url)
                year = ref.year or 9999
                return 400 + (year - 2000) * 5 + min(ref.page_no, 9)
            except ValueError:
                return 800
        if parser.is_article_url(url):
            return 900
        return 5000

    def add_url(
        self,
        url: str,
        *,
        depth: int,
        parent: str = "",
        priority: int | None = None,
    ) -> bool:
        normalized = canonicalize_url(url)
        if not normalized or not self.allowed(normalized, depth):
            return False
        target_priority = priority if priority is not None else self.priority_for_url(normalized)
        inserted = db.enqueue_url(
            self.conn,
            url=normalized,
            depth=depth,
            parent_url=parent,
            priority=target_priority,
        )
        if not inserted and priority is not None:
            # 已存在的版块分页也要按“最老优先”更新顺序。
            db.set_priority(self.conn, normalized, target_priority)
        return inserted

    def reprioritize_queue(self) -> int:
        if self.order != "oldest":
            return 0
        rows = self.conn.execute(
            "SELECT url FROM crawl_urls WHERE status = 'pending'"
        ).fetchall()
        for row in rows:
            db.set_priority(self.conn, row["url"], self.priority_for_url(row["url"]))
        self.conn.commit()
        return len(rows)

    @staticmethod
    def board_total_pages(html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        selector = soup.select_one(".page_selec")
        text = selector.get_text(" ", strip=True) if selector else ""
        match = re.search(r"/\s*(\d{1,5})", text)
        if not match:
            match = re.search(r"/\s*(\d{1,5})", soup.get_text(" ", strip=True))
        if not match:
            return 0
        return int(match.group(1))

    @staticmethod
    def board_has_threads(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.select('a[href*="archives"]'):
            href = anchor.get("href") or ""
            if "788978" in href:
                continue
            if re.search(r"archives/\d{4}/\d{4,}\.shtml", href):
                return True
        return False

    def board_last_page(self, url: str, total: int) -> int:
        """站点的 /250 页数可能包含空页，用二分探测最后一个有帖子的页。"""
        if total < 2:
            return total

        def has_threads(page_no: int) -> bool:
            try:
                response = self.fetch(board_page_url(url, page_no))
                html = parser.decode_html(
                    response.content, response.headers.get("Content-Type", "")
                )
                return self.board_has_threads(html)
            except (FetchError, NotFound):
                return False

        if has_threads(total):
            return total
        low, high = 1, total - 1
        while low < high:
            mid = (low + high + 1) // 2
            if has_threads(mid):
                low = mid
            else:
                high = mid - 1
        return low

    def discover_board_pages(self, html: str, url: str, depth: int) -> None:
        if not is_board_list_url(url) or board_page_number(url) != 1:
            return
        total = self.board_total_pages(html)
        if total < 2:
            return
        last = self.board_last_page(url, total)
        if last < 2:
            return
        # 页码越大越老：最后一页排到最前，然后倒退。
        for page_no in range(2, last + 1):
            target = board_page_url(url, page_no)
            self.add_url(
                target,
                depth=depth,
                parent=url,
                priority=100 + (last - page_no) * 100,
            )
        self.log(f"  版块标称 {total} 页，实际最后一页 {last}，已按最老优先排队", "ok")

    # ------------------------------------------------------------------ fetch
    def fetch(self, url: str, *, referer: str = "", accept: str = "") -> requests.Response:
        headers = {"Referer": referer} if referer else {}
        if accept:
            headers["Accept"] = accept
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    headers=headers,
                    allow_redirects=True,
                )
                if response.status_code == 404:
                    raise NotFound(f"404 {url}")
                if response.status_code in (429, 500, 502, 503, 504):
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = min(30.0, 2.0 ** attempt + random.random())
                    self.log(f"HTTP {response.status_code}，{wait:.1f}s 后重试：{url}", "warn")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response
            except NotFound:
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    wait = min(20.0, 2.0 ** attempt + random.random())
                    self.log(f"请求失败，{wait:.1f}s 后重试：{url}", "warn")
                    time.sleep(wait)
        raise FetchError(str(last_error or f"请求失败: {url}"))

    # ------------------------------------------------------------------ images
    def ingest_image(
        self,
        image: parser.ParsedImage,
        *,
        album_id: int,
        page_id: int,
        post_id: int,
        page_title: str,
        caption: str,
        posted_at: str,
        page_url: str,
        page_no: int,
    ) -> bool:
        """下载/去重一张图片。返回 False 表示达到 --max-images 上限。"""
        self.images_seen += 1
        source_url = image.url
        download_url = image.link_url or image.url
        archived_at = db.utcnow()

        if source_url in self.url_cache:
            image_id = self.url_cache[source_url]
            self.images_duplicate += 1
            db.add_image_source(
                self.conn,
                image_id=image_id,
                album_id=album_id,
                page_id=page_id,
                post_id=post_id,
                source_url=source_url,
                link_url=image.link_url,
                position=image.position,
                page_no=page_no,
                page_title=page_title,
                posted_at=posted_at,
                archived_at=archived_at,
            )
            return True

        if self.max_images and self.images_downloaded >= self.max_images:
            self.stop_reason = "image_limit"
            return False

        try:
            response = self.fetch(download_url, referer=page_url, accept="image/*")
        except (FetchError, NotFound) as exc:
            self.image_errors += 1
            self.log(f"图片失败：{download_url} ({exc})", "warn")
            return True

        content_type = (response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            if "octet-stream" not in content_type:
                self.images_skipped += 1
                return True

        data = response.content
        if not data:
            self.images_skipped += 1
            return True
        if len(data) > MAX_IMAGE_BYTES:
            self.images_skipped += 1
            self.log(f"图片超过 40MB，跳过：{download_url}", "warn")
            return True

        try:
            meta = probe_image(data)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            self.images_skipped += 1
            self.log(f"不是有效图片，跳过：{download_url} ({exc})", "warn")
            return True

        if meta.width < self.min_width or meta.height < self.min_height or len(data) < self.min_bytes:
            self.images_skipped += 1
            return True

        sha = hashlib.sha256(data).hexdigest()
        rel_path = f"images/{sha[:2]}/{sha}.{meta.ext}"
        thumb_path = f"thumbs/{sha}.jpg"
        absolute = self.data_dir / rel_path
        thumb_absolute = self.data_dir / thumb_path

        existing = db.get_image_by_hash(self.conn, sha)
        if existing is None:
            absolute.parent.mkdir(parents=True, exist_ok=True)
            if not absolute.exists():
                temp = absolute.with_suffix(absolute.suffix + ".part")
                temp.write_bytes(data)
                os.replace(temp, absolute)
            if not thumb_absolute.exists():
                try:
                    write_thumbnail(data, thumb_absolute)
                except Exception as exc:
                    self.log(f"缩略图生成失败：{download_url} ({exc})", "warn")
                    thumb_path = None
        else:
            # 文件丢了就补回来；缩略图缺失也补。
            if not absolute.exists():
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.write_bytes(data)
            if not thumb_absolute.exists():
                try:
                    write_thumbnail(data, thumb_absolute)
                except Exception:
                    thumb_path = existing["thumb_path"]

        try:
            timestamp = datetime.fromisoformat(archived_at).timestamp()
            if absolute.exists():
                os.utime(absolute, (timestamp, timestamp))
        except (OSError, ValueError):
            pass

        image_id, is_new = db.insert_image(
            self.conn,
            sha256=sha,
            rel_path=rel_path,
            thumb_path=thumb_path,
            ext=meta.ext,
            mime=meta.mime,
            width=meta.width,
            height=meta.height,
            bytes_=len(data),
            page_title=page_title,
            caption=caption,
            alt_text=image.alt,
            posted_at=posted_at,
            archived_at=archived_at,
            exif_taken_at=meta.exif_taken_at,
            http_last_modified=response.headers.get("Last-Modified", ""),
        )
        db.add_image_source(
            self.conn,
            image_id=image_id,
            album_id=album_id,
            page_id=page_id,
            post_id=post_id,
            source_url=source_url,
            link_url=image.link_url,
            position=image.position,
            page_no=page_no,
            page_title=page_title,
            posted_at=posted_at,
            archived_at=archived_at,
        )
        self.url_cache[source_url] = image_id
        self.images_downloaded += 1
        if is_new:
            self.images_new += 1
        else:
            self.images_duplicate += 1
        self.conn.commit()
        return True

    # ------------------------------------------------------------------ pages
    def handle_thread(
        self, item: dict, final_url: str, html: str, response: requests.Response
    ) -> int | None:
        ref = parser.parse_thread_url(final_url)
        source_key = f"thread:{ref.source_id}"
        canonical = parser.build_page_url(final_url, 1)
        album = db.get_album_by_key(self.conn, source_key)

        if ref.page_no > 1 and album is None:
            self.add_url(canonical, depth=item["depth"], parent=final_url, priority=20)
            if item["attempts"] >= 3:
                db.finish_url(
                    self.conn,
                    url=item["url"],
                    status="skipped",
                    http_status=response.status_code,
                    content_type=response.headers.get("Content-Type", ""),
                    error="首帖多次未能建立相册",
                )
            else:
                # 先抓首帖建立相册，本页稍后重新排队；不要直接永久跳过。
                db.finish_url(
                    self.conn,
                    url=item["url"],
                    status="skipped",
                    http_status=response.status_code,
                    content_type=response.headers.get("Content-Type", ""),
                    error="等待首帖建立相册",
                )
                db.requeue_url(
                    self.conn,
                    item["url"],
                    priority=45,
                    error="等待首帖建立相册",
                )
            self.conn.commit()
            return None

        parsed = parser.parse_thread_page(html, final_url)
        if not parsed.posts:
            db.finish_url(
                self.conn,
                url=item["url"],
                status="skipped",
                http_status=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                title=parsed.title,
                error="页面没有可解析的楼层",
            )
            self.conn.commit()
            return None

        if ref.page_no == 1:
            if parsed.reply_count:
                page_count = parser.estimate_page_count(len(parsed.posts), parsed.reply_count)
            else:
                # 老模板的跟帖数由 JS 写入，静态页取不到；满 20 帖时先探测下一页。
                page_count = 2 if len(parsed.posts) >= 20 else 1
        else:
            page_count = max(int(album["page_count"] if album else 1), ref.page_no)
        album_id = db.upsert_album(
            self.conn,
            kind="thread",
            source_key=source_key,
            url=canonical,
            title=parsed.title,
            raw_title=parsed.raw_title,
            section=parsed.board,
            board=parsed.board,
            author=parsed.author,
            source_year=ref.year,
            posted_at=parsed.posted_at,
            reply_count=parsed.reply_count,
            page_count=page_count,
            archived_at=db.utcnow(),
        )
        self.album_ids.add(album_id)
        album = db.get_album(self.conn, album_id) or album

        page_id = db.upsert_page(
            self.conn,
            album_id=album_id,
            page_no=ref.page_no,
            url=final_url,
            title=parsed.title,
            http_status=response.status_code,
        )
        page_title = (album or {}).get("title") or parsed.title

        for post in parsed.posts:
            post_id = db.upsert_post(
                self.conn,
                album_id=album_id,
                page_id=page_id,
                source_id=post.source_id,
                floor=post.floor,
                author=post.author,
                posted_at=post.posted_at,
                body_text=post.body_text,
                source_url=f"{final_url}#ssforum{post.source_id}",
            )
            caption = f"{post.floor} · {post.author}" if post.author else post.floor
            for image in post.images:
                if not self.ingest_image(
                    image,
                    album_id=album_id,
                    page_id=page_id,
                    post_id=post_id,
                    page_title=page_title,
                    caption=caption,
                    posted_at=post.posted_at,
                    page_url=final_url,
                    page_no=ref.page_no,
                ):
                    break
            if self.stop_reason:
                break

        if ref.page_no == 1:
            for page_no in range(2, page_count + 1):
                self.add_url(
                    parser.build_page_url(canonical, page_no),
                    depth=item["depth"],
                    parent=final_url,
                )
        elif not parsed.reply_count and len(parsed.posts) >= 20 and ref.page_no < 100:
            # 未知总页数时，逐页探测，直到某一页不满 20 帖或 404。
            self.add_url(
                parser.build_page_url(canonical, ref.page_no + 1),
                depth=item["depth"],
                parent=final_url,
            )

        db.finish_url(
            self.conn,
            url=item["url"],
            status="done",
            http_status=response.status_code,
            content_type=response.headers.get("Content-Type", ""),
            title=parsed.title,
            album_id=album_id,
        )
        self.conn.commit()
        return album_id

    def handle_article(
        self, item: dict, final_url: str, html: str, response: requests.Response
    ) -> int | None:
        parsed = parser.parse_article_page(html, final_url)
        if not parsed.images:
            db.finish_url(
                self.conn,
                url=item["url"],
                status="done",
                http_status=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                title=parsed.title,
            )
            self.conn.commit()
            return None

        source_key = "article:" + hashlib.sha1(final_url.encode("utf-8")).hexdigest()[:16]
        year_match = re.search(r"(20\d{2})", parsed.posted_at or final_url)
        source_year = int(year_match.group(1)) if year_match else None
        album_id = db.upsert_album(
            self.conn,
            kind="article",
            source_key=source_key,
            url=final_url,
            title=parsed.title,
            raw_title=parsed.raw_title,
            section=parsed.section or "资讯",
            author=parsed.author,
            source_year=source_year,
            posted_at=parsed.posted_at,
            page_count=1,
            archived_at=db.utcnow(),
        )
        self.album_ids.add(album_id)
        page_id = db.upsert_page(
            self.conn,
            album_id=album_id,
            page_no=1,
            url=final_url,
            title=parsed.title,
            http_status=response.status_code,
        )
        post_id = db.upsert_post(
            self.conn,
            album_id=album_id,
            page_id=page_id,
            source_id="main",
            floor="正文",
            author=parsed.author,
            posted_at=parsed.posted_at,
            body_text=parsed.body_text,
            source_url=final_url,
        )
        caption = f"正文 · {parsed.author}" if parsed.author else "正文"
        for image in parsed.images:
            if not self.ingest_image(
                image,
                album_id=album_id,
                page_id=page_id,
                post_id=post_id,
                page_title=parsed.title,
                caption=caption,
                posted_at=parsed.posted_at,
                page_url=final_url,
                page_no=1,
            ):
                break

        if db.count_album_images(self.conn, album_id) == 0:
            self.conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            self.album_ids.discard(album_id)
            album_id = None

        db.finish_url(
            self.conn,
            url=item["url"],
            status="done",
            http_status=response.status_code,
            content_type=response.headers.get("Content-Type", ""),
            title=parsed.title,
            album_id=album_id,
        )
        self.conn.commit()
        return album_id

    def process(self, item: dict) -> None:
        url = item["url"]
        try:
            response = self.fetch(url)
        except NotFound:
            db.finish_url(self.conn, url=url, status="skipped", error="404")
            self.conn.commit()
            return
        except FetchError as exc:
            self.errors += 1
            db.finish_url(self.conn, url=url, status="failed", error=str(exc))
            self.conn.commit()
            self.log(f"页面失败：{url} ({exc})", "error")
            return

        content_type = response.headers.get("Content-Type", "")
        lower_type = content_type.lower()
        if "text/html" not in lower_type and "xhtml" not in lower_type:
            if not (not lower_type and response.content[:200].lstrip().lower().startswith(b"<")):
                db.finish_url(
                    self.conn,
                    url=url,
                    status="skipped",
                    http_status=response.status_code,
                    content_type=content_type,
                    error="非 HTML 页面",
                )
                self.conn.commit()
                return

        final_url = canonicalize_url(response.url) or url
        html = parser.decode_html(response.content, content_type)
        title = parser.canonical_title(html)
        self.pages_fetched += 1

        self.discover_board_pages(html, final_url, item["depth"])

        for link in parser.extract_links(html, final_url):
            self.add_url(
                link.url,
                depth=item["depth"] + 1,
                parent=final_url,
            )

        if parser.is_thread_url(final_url) or parser.is_thread_url(url):
            self.log(f"[{self.pages_fetched}] 论坛帖 {title[:60]}", "info")
            album_id = self.handle_thread(item, final_url, html, response)
        elif parser.is_article_url(final_url) or self.looks_like_article(html, final_url):
            self.log(f"[{self.pages_fetched}] 文章 {title[:60]}", "info")
            album_id = self.handle_article(item, final_url, html, response)
        else:
            db.finish_url(
                self.conn,
                url=url,
                status="done",
                http_status=response.status_code,
                content_type=content_type,
                title=title,
            )
            self.conn.commit()
            self.log(f"[{self.pages_fetched}] 浏览 {title[:60]}", "info")
            return

        if album_id:
            self.log(
                f"    相册 #{album_id}，累计 {self.images_new} 新图 / "
                f"{self.images_duplicate} 重复 / {len(self.album_ids)} 相册",
                "ok",
            )

    @staticmethod
    def looks_like_article(html: str, url: str) -> bool:
        from bs4 import BeautifulSoup
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        if path.endswith("/") or not re.search(r"\d{3,}", path):
            return False
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("article.page, article.article-content")
        if node is None:
            return False
        return len(node.select("h1")) == 1 and len(node.select("p")) >= 2

    # ------------------------------------------------------------------ run
    def run(self, seeds: list[str], *, retry_failed: bool = False) -> dict:
        self.load_robots()
        if self.robots_delay:
            self.limiter.interval = max(self.limiter.interval, self.robots_delay)
        db.reset_in_progress(self.conn)
        waiting = db.retry_waiting_threads(self.conn)
        if waiting:
            self.log(f"重新排队 {waiting} 个等待首帖的分页")
        if retry_failed:
            count = db.retry_failed(self.conn)
            if count:
                self.log(f"重新排队 {count} 个失败页面")
        if self.order == "oldest":
            count = self.reprioritize_queue()
            if count:
                self.log(f"按最老优先重排 {count} 个待抓 URL")
        self.conn.commit()

        for seed in seeds:
            normalized = canonicalize_url(seed)
            if not normalized:
                self.log(f"忽略站外种子：{seed}", "warn")
                continue
            self.add_url(normalized, depth=0, priority=10)
            if (
                self.order == "oldest"
                and is_board_list_url(normalized)
                and board_page_number(normalized) == 1
            ):
                # 版块第 1 页只用于读总页数；即使以前抓过，也再看一次。
                db.requeue_any(self.conn, normalized, priority=30)
        self.conn.commit()

        crawl_id = db.start_site_crawl(
            self.conn, seed=" | ".join(seeds), scope=self.scope
        )
        self.conn.commit()
        status = "completed"
        message = ""
        try:
            while True:
                if self.stop_reason:
                    status = self.stop_reason
                    break
                if self.max_pages and self.pages_fetched >= self.max_pages:
                    status = "page_limit"
                    break
                item = db.claim_next_url(self.conn)
                if item is None:
                    status = "completed"
                    break
                if not self.allowed(item["url"], item["depth"]):
                    db.finish_url(
                        self.conn, url=item["url"], status="skipped", error="不在当前抓取范围"
                    )
                    self.conn.commit()
                    continue
                self.process(item)
                self.conn.commit()
        except KeyboardInterrupt:
            status = "interrupted"
            message = "用户中断，队列已保留，可重新运行续爬"
            self.log(message, "warn")
        except Exception as exc:
            status = "error"
            message = f"{type(exc).__name__}: {exc}"
            self.errors += 1
            self.log(f"抓取异常：{message}", "error")
            raise
        finally:
            db.reset_in_progress(self.conn)
            stats = db.queue_stats(self.conn)
            db.finish_site_crawl(
                self.conn,
                crawl_id,
                status=status,
                pages_fetched=self.pages_fetched,
                pages_discovered=sum(stats.values()),
                albums_found=len(self.album_ids),
                images_seen=self.images_seen,
                images_new=self.images_new,
                images_duplicate=self.images_duplicate,
                errors=self.errors + self.image_errors,
                message=message,
            )
            self.conn.commit()
            summary = {
                "status": status,
                "pages_fetched": self.pages_fetched,
                "albums": len(self.album_ids),
                "images_seen": self.images_seen,
                "images_new": self.images_new,
                "images_duplicate": self.images_duplicate,
                "images_skipped": self.images_skipped,
                "image_errors": self.image_errors,
                "queue": stats,
            }
            self.log(
                "完成：{pages_fetched} 页 / {albums} 相册 / {images_new} 新图 / "
                "{images_duplicate} 重复 / {images_skipped} 跳过 / 队列 {queue}".format(**summary),
                "ok",
            )
            self.conn.close()
        return summary


def build_arg_parser() -> argparse.ArgumentParser:
    arg = argparse.ArgumentParser(
        description="联商网全站图片存档爬虫（支持续爬、内容去重、存档时间）"
    )
    arg.add_argument("seeds", nargs="*", help="种子 URL；留空则按 --scope 使用默认入口")
    arg.add_argument("--scope", choices=("site", "forum", "news"), default="site")
    arg.add_argument(
        "--order",
        choices=("oldest", "newest"),
        default="oldest",
        help="抓取顺序：oldest 按年份从老到新（默认），newest 按发现顺序",
    )
    arg.add_argument("--data-dir", default="data", help="数据目录（默认 ./data）")
    arg.add_argument("--max-pages", type=int, default=1000, help="最多抓取页面数，0 为不限")
    arg.add_argument("--max-depth", type=int, default=10, help="最大链接深度")
    arg.add_argument("--max-images", type=int, default=0, help="最多下载图片数，0 为不限")
    arg.add_argument("--delay", type=float, default=2.0, help="请求间隔秒数（默认 2.0）")
    arg.add_argument("--timeout", type=float, default=30.0)
    arg.add_argument("--retries", type=int, default=3)
    arg.add_argument("--min-width", type=int, default=200)
    arg.add_argument("--min-height", type=int, default=150)
    arg.add_argument("--min-bytes", type=int, default=3000)
    arg.add_argument("--retry-failed", action="store_true", help="把失败页面重新排队")
    return arg


def default_seeds(scope: str, order: str = "oldest") -> list[str]:
    boards = [FORUM_BOARD_URL.format(board_id=board_id) for board_id in BOARD_IDS]
    if scope == "forum":
        return boards if order == "oldest" else ["http://www.linkshop.com/club/"]
    if scope == "news":
        return ["http://www.linkshop.com/news/"]
    if order == "oldest":
        # 先从各版块第 1 页读总页数，随后队列会自动把最后一页排到最前。
        return ["http://www.linkshop.com/"] + boards
    return ["http://www.linkshop.com/"]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_arg_parser().parse_args(argv)
    seeds = args.seeds or default_seeds(args.scope, args.order)
    data_dir = Path(args.data_dir).expanduser().resolve()
    crawler = SiteCrawler(
        data_dir=data_dir,
        scope=args.scope,
        order=args.order,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        max_images=args.max_images,
        min_width=args.min_width,
        min_height=args.min_height,
        min_bytes=args.min_bytes,
    )
    crawler.log(f"数据目录：{data_dir}")
    crawler.log(f"范围：{args.scope}；顺序：{args.order}；种子：{', '.join(seeds)}")
    crawler.run(seeds, retry_failed=args.retry_failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
