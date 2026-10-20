"""SQLite 数据层：全站相册、页面、楼层、图片来源与可续爬队列。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL DEFAULT 'thread'
                  CHECK (kind IN ('thread', 'article', 'page')),
    source_key    TEXT NOT NULL UNIQUE,
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    raw_title     TEXT,
    section       TEXT,
    board         TEXT,
    author        TEXT,
    source_year   INTEGER,
    posted_at     TEXT,
    reply_count   INTEGER NOT NULL DEFAULT 0,
    page_count    INTEGER NOT NULL DEFAULT 0,
    archived_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    page_no       INTEGER NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT,
    fetched_at    TEXT,
    http_status   INTEGER,
    UNIQUE(album_id, page_no)
);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    page_id       INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    source_id     TEXT NOT NULL,
    floor         TEXT,
    author        TEXT,
    posted_at     TEXT,
    body_text     TEXT,
    source_url    TEXT,
    UNIQUE(album_id, source_id)
);

CREATE TABLE IF NOT EXISTS images (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256              TEXT NOT NULL UNIQUE,
    rel_path            TEXT NOT NULL UNIQUE,
    thumb_path          TEXT,
    ext                 TEXT NOT NULL,
    mime                TEXT,
    width               INTEGER,
    height              INTEGER,
    bytes               INTEGER,
    page_title          TEXT NOT NULL,
    caption             TEXT,
    alt_text            TEXT,
    posted_at           TEXT,
    archived_at         TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    exif_taken_at       TEXT,
    http_last_modified  TEXT,
    status              TEXT NOT NULL DEFAULT 'ok',
    error               TEXT
);

CREATE TABLE IF NOT EXISTS image_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    album_id      INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    page_id       INTEGER REFERENCES pages(id) ON DELETE SET NULL,
    post_id       INTEGER REFERENCES posts(id) ON DELETE CASCADE,
    source_url    TEXT NOT NULL,
    link_url      TEXT,
    position      INTEGER NOT NULL DEFAULT 0,
    page_no       INTEGER,
    page_title    TEXT NOT NULL,
    posted_at     TEXT,
    archived_at   TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(image_id, post_id, source_url)
);

CREATE TABLE IF NOT EXISTS crawl_urls (
    url             TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'skipped')),
    depth           INTEGER NOT NULL DEFAULT 0,
    parent_url      TEXT,
    priority        INTEGER NOT NULL DEFAULT 100,
    attempts        INTEGER NOT NULL DEFAULT 0,
    http_status     INTEGER,
    content_type    TEXT,
    title           TEXT,
    album_id        INTEGER REFERENCES albums(id) ON DELETE SET NULL,
    discovered_at   TEXT NOT NULL,
    fetched_at      TEXT,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS site_crawls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    seed              TEXT NOT NULL,
    scope             TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL DEFAULT 'running',
    pages_fetched     INTEGER NOT NULL DEFAULT 0,
    pages_discovered  INTEGER NOT NULL DEFAULT 0,
    albums_found      INTEGER NOT NULL DEFAULT 0,
    images_seen       INTEGER NOT NULL DEFAULT 0,
    images_new        INTEGER NOT NULL DEFAULT 0,
    images_duplicate  INTEGER NOT NULL DEFAULT 0,
    errors            INTEGER NOT NULL DEFAULT 0,
    message           TEXT
);

CREATE INDEX IF NOT EXISTS idx_pages_album ON pages(album_id);
CREATE INDEX IF NOT EXISTS idx_posts_album ON posts(album_id);
CREATE INDEX IF NOT EXISTS idx_posts_page ON posts(page_id);
CREATE INDEX IF NOT EXISTS idx_sources_image ON image_sources(image_id);
CREATE INDEX IF NOT EXISTS idx_sources_album ON image_sources(album_id);
CREATE INDEX IF NOT EXISTS idx_sources_post ON image_sources(post_id);
CREATE INDEX IF NOT EXISTS idx_images_archived ON images(archived_at DESC);
CREATE INDEX IF NOT EXISTS idx_albums_year ON albums(source_year);
CREATE INDEX IF NOT EXISTS idx_albums_section ON albums(section);
CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_urls(status, priority, depth);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    create_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# 相册 / 页面 / 楼层
# ---------------------------------------------------------------------------


def upsert_album(
    conn: sqlite3.Connection,
    *,
    kind: str,
    source_key: str,
    url: str,
    title: str,
    raw_title: str = "",
    section: str = "",
    board: str = "",
    author: str = "",
    source_year: int | None = None,
    posted_at: str = "",
    reply_count: int = 0,
    page_count: int = 0,
    archived_at: str | None = None,
) -> int:
    conn.execute(
        """
        INSERT INTO albums
            (kind, source_key, url, title, raw_title, section, board, author,
             source_year, posted_at, reply_count, page_count, archived_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            url = excluded.url,
            title = excluded.title,
            raw_title = excluded.raw_title,
            section = excluded.section,
            board = excluded.board,
            author = COALESCE(NULLIF(albums.author, ''), excluded.author),
            source_year = COALESCE(albums.source_year, excluded.source_year),
            posted_at = COALESCE(NULLIF(albums.posted_at, ''), excluded.posted_at),
            reply_count = excluded.reply_count,
            page_count = MAX(albums.page_count, excluded.page_count),
            archived_at = COALESCE(albums.archived_at, excluded.archived_at)
        """,
        (
            kind,
            source_key,
            url,
            title,
            raw_title,
            section,
            board,
            author,
            source_year,
            posted_at,
            reply_count,
            page_count,
            archived_at or utcnow(),
        ),
    )
    row = conn.execute(
        "SELECT id FROM albums WHERE source_key = ?", (source_key,)
    ).fetchone()
    return int(row["id"])


def get_album_by_url(conn: sqlite3.Connection, url: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM albums WHERE url = ?", (url,)).fetchone()
    return dict(row) if row else None


def get_album_by_key(conn: sqlite3.Connection, source_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM albums WHERE source_key = ?", (source_key,)).fetchone()
    return dict(row) if row else None


def upsert_page(
    conn: sqlite3.Connection,
    *,
    album_id: int,
    page_no: int,
    url: str,
    title: str = "",
    fetched_at: str | None = None,
    http_status: int = 200,
) -> int:
    conn.execute(
        """
        INSERT INTO pages (album_id, page_no, url, title, fetched_at, http_status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_id, page_no) DO UPDATE SET
            url = excluded.url,
            title = excluded.title,
            fetched_at = excluded.fetched_at,
            http_status = excluded.http_status
        """,
        (album_id, page_no, url, title, fetched_at or utcnow(), http_status),
    )
    row = conn.execute(
        "SELECT id FROM pages WHERE album_id = ? AND page_no = ?", (album_id, page_no)
    ).fetchone()
    return int(row["id"])


def upsert_post(
    conn: sqlite3.Connection,
    *,
    album_id: int,
    page_id: int,
    source_id: str,
    floor: str,
    author: str,
    posted_at: str,
    body_text: str,
    source_url: str,
) -> int:
    conn.execute(
        """
        INSERT INTO posts
            (album_id, page_id, source_id, floor, author, posted_at, body_text, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_id, source_id) DO UPDATE SET
            page_id = excluded.page_id,
            floor = excluded.floor,
            author = excluded.author,
            posted_at = excluded.posted_at,
            body_text = excluded.body_text,
            source_url = excluded.source_url
        """,
        (
            album_id,
            page_id,
            source_id,
            floor,
            author,
            posted_at,
            body_text,
            source_url,
        ),
    )
    row = conn.execute(
        "SELECT id FROM posts WHERE album_id = ? AND source_id = ?",
        (album_id, source_id),
    ).fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# 图片与来源
# ---------------------------------------------------------------------------


def get_image_by_hash(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM images WHERE sha256 = ?", (sha256,)).fetchone()


def insert_image(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    rel_path: str,
    thumb_path: str | None,
    ext: str,
    mime: str,
    width: int,
    height: int,
    bytes_: int,
    page_title: str,
    caption: str,
    alt_text: str | None,
    posted_at: str = "",
    archived_at: str | None = None,
    exif_taken_at: str = "",
    http_last_modified: str = "",
) -> tuple[int, bool]:
    """返回 (image_id, is_new)。sha256 冲突时复用已有记录并刷新 last_seen_at。"""
    now = archived_at or utcnow()
    try:
        cursor = conn.execute(
            """
            INSERT INTO images
                (sha256, rel_path, thumb_path, ext, mime, width, height, bytes,
                 page_title, caption, alt_text, posted_at, archived_at, last_seen_at,
                 exif_taken_at, http_last_modified, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')
            """,
            (
                sha256,
                rel_path,
                thumb_path,
                ext,
                mime,
                width,
                height,
                bytes_,
                page_title,
                caption,
                alt_text,
                posted_at,
                now,
                now,
                exif_taken_at,
                http_last_modified,
            ),
        )
        return int(cursor.lastrowid), True
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT * FROM images WHERE sha256 = ? OR rel_path = ? LIMIT 1",
            (sha256, rel_path),
        ).fetchone()
        if row is None:
            raise
        conn.execute(
            """
            UPDATE images SET
                last_seen_at = ?,
                posted_at = COALESCE(NULLIF(posted_at, ''), ?),
                exif_taken_at = COALESCE(NULLIF(exif_taken_at, ''), ?),
                http_last_modified = COALESCE(NULLIF(http_last_modified, ''), ?)
            WHERE id = ?
            """,
            (now, posted_at, exif_taken_at, http_last_modified, row["id"]),
        )
        return int(row["id"]), False


def add_image_source(
    conn: sqlite3.Connection,
    *,
    image_id: int,
    album_id: int,
    page_id: int,
    post_id: int,
    source_url: str,
    link_url: str | None,
    position: int,
    page_no: int,
    page_title: str,
    posted_at: str,
    archived_at: str,
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO image_sources
            (image_id, album_id, page_id, post_id, source_url, link_url, position,
             page_no, page_title, posted_at, archived_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_id,
            album_id,
            page_id,
            post_id,
            source_url,
            link_url,
            position,
            page_no,
            page_title,
            posted_at,
            archived_at,
        ),
    )
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# 抓取队列 / 抓取记录
# ---------------------------------------------------------------------------


def enqueue_url(
    conn: sqlite3.Connection,
    *,
    url: str,
    depth: int,
    parent_url: str = "",
    priority: int = 100,
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO crawl_urls
            (url, status, depth, parent_url, priority, discovered_at)
        VALUES (?, 'pending', ?, ?, ?, ?)
        """,
        (url, depth, parent_url, priority, utcnow()),
    )
    return cursor.rowcount > 0


def claim_next_url(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM crawl_urls
            WHERE status = 'pending'
            ORDER BY priority ASC, depth ASC, discovered_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE crawl_urls SET status = 'in_progress', attempts = attempts + 1 WHERE url = ?",
            (row["url"],),
        )
        conn.commit()
        data = dict(row)
        data["status"] = "in_progress"
        return data
    except Exception:
        conn.rollback()
        raise


def finish_url(
    conn: sqlite3.Connection,
    *,
    url: str,
    status: str,
    http_status: int | None = None,
    content_type: str = "",
    title: str = "",
    album_id: int | None = None,
    error: str = "",
) -> None:
    conn.execute(
        """
        UPDATE crawl_urls SET
            status = ?, http_status = ?, content_type = ?, title = ?,
            album_id = COALESCE(?, album_id), fetched_at = ?, last_error = ?
        WHERE url = ?
        """,
        (
            status,
            http_status,
            content_type,
            title,
            album_id,
            utcnow(),
            error[:2000],
            url,
        ),
    )


def reset_in_progress(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE crawl_urls SET status = 'pending' WHERE status = 'in_progress'"
    )
    return cursor.rowcount


def retry_failed(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "UPDATE crawl_urls SET status = 'pending', last_error = '' WHERE status = 'failed'"
    )
    return cursor.rowcount


def set_priority(
    conn: sqlite3.Connection, url: str, priority: int, *, only_pending: bool = True
) -> None:
    if only_pending:
        conn.execute(
            "UPDATE crawl_urls SET priority = ? WHERE url = ? AND status = 'pending'",
            (priority, url),
        )
    else:
        conn.execute(
            "UPDATE crawl_urls SET priority = ? WHERE url = ?", (priority, url)
        )


def retry_waiting_threads(conn: sqlite3.Connection) -> int:
    """重新排队“等待首帖建立相册”的后续分页。"""
    cursor = conn.execute(
        """
        UPDATE crawl_urls
        SET status = 'pending', last_error = '', priority = 45
        WHERE status = 'skipped' AND last_error LIKE '等待首帖%'
        """
    )
    return cursor.rowcount


def requeue_url(conn: sqlite3.Connection, url: str, *, priority: int = 45, error: str = "") -> None:
    conn.execute(
        """
        UPDATE crawl_urls SET status = 'pending', priority = ?, last_error = ?
        WHERE url = ? AND status IN ('skipped', 'failed')
        """,
        (priority, error[:2000], url),
    )


def requeue_any(conn: sqlite3.Connection, url: str, *, priority: int = 30) -> None:
    """强制重新抓取一个页面（用于版块第 1 页重新发现历史分页）。"""
    conn.execute(
        """
        UPDATE crawl_urls SET status = 'pending', priority = ?, last_error = ''
        WHERE url = ?
        """,
        (priority, url),
    )


def queue_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM crawl_urls GROUP BY status"
    ).fetchall()
    stats = {"pending": 0, "in_progress": 0, "done": 0, "failed": 0, "skipped": 0}
    for row in rows:
        stats[row["status"]] = int(row["n"])
    return stats


def start_site_crawl(conn: sqlite3.Connection, *, seed: str, scope: str) -> int:
    cursor = conn.execute(
        "INSERT INTO site_crawls (seed, scope, started_at, status) VALUES (?, ?, ?, 'running')",
        (seed, scope, utcnow()),
    )
    return int(cursor.lastrowid)


def finish_site_crawl(
    conn: sqlite3.Connection,
    crawl_id: int,
    *,
    status: str,
    pages_fetched: int,
    pages_discovered: int,
    albums_found: int,
    images_seen: int,
    images_new: int,
    images_duplicate: int,
    errors: int,
    message: str = "",
) -> None:
    conn.execute(
        """
        UPDATE site_crawls SET
            finished_at = ?, status = ?, pages_fetched = ?, pages_discovered = ?,
            albums_found = ?, images_seen = ?, images_new = ?,
            images_duplicate = ?, errors = ?, message = ?
        WHERE id = ?
        """,
        (
            utcnow(),
            status,
            pages_fetched,
            pages_discovered,
            albums_found,
            images_seen,
            images_new,
            images_duplicate,
            errors,
            message[:2000],
            crawl_id,
        ),
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM albums a
             WHERE EXISTS (SELECT 1 FROM image_sources s WHERE s.album_id = a.id)) AS albums,
            (SELECT COUNT(*) FROM images WHERE status = 'ok') AS images,
            (SELECT COUNT(*) FROM image_sources) AS sources,
            (SELECT COUNT(DISTINCT image_id) FROM image_sources) AS used_images,
            (SELECT COALESCE(SUM(bytes), 0) FROM images) AS bytes,
            (SELECT COUNT(*) FROM posts) AS posts,
            (SELECT COUNT(*) FROM pages) AS pages,
            (SELECT MIN(archived_at) FROM images) AS archived_from,
            (SELECT MAX(archived_at) FROM images) AS archived_to,
            (SELECT COUNT(*) FROM crawl_urls) AS discovered
        """
    ).fetchone()
    data = dict(row)
    data["duplicates"] = max(0, data["sources"] - data["used_images"])
    return data


def list_years(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            a.source_year AS year,
            COUNT(*) AS albums,
            (SELECT COUNT(DISTINCT s.image_id)
             FROM image_sources s
             JOIN albums a2 ON a2.id = s.album_id
             WHERE a2.source_year = a.source_year) AS images
        FROM albums a
        WHERE a.source_year IS NOT NULL
          AND EXISTS (SELECT 1 FROM image_sources s0 WHERE s0.album_id = a.id)
        GROUP BY a.source_year
        ORDER BY a.source_year DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def list_sections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            COALESCE(a.section, '未分类') AS section,
            COUNT(*) AS albums,
            (SELECT COUNT(DISTINCT s.image_id)
             FROM image_sources s
             JOIN albums a2 ON a2.id = s.album_id
             WHERE COALESCE(a2.section, '未分类') = COALESCE(a.section, '未分类')) AS images
        FROM albums a
        WHERE EXISTS (SELECT 1 FROM image_sources s0 WHERE s0.album_id = a.id)
        GROUP BY COALESCE(a.section, '未分类')
        ORDER BY images DESC, albums DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


ALBUM_SELECT = """
SELECT
    a.*,
    (SELECT COUNT(DISTINCT s.image_id)
     FROM image_sources s WHERE s.album_id = a.id) AS image_count,
    (SELECT COUNT(*) FROM image_sources s WHERE s.album_id = a.id) AS source_count,
    (SELECT COALESCE(SUM(i.bytes), 0)
     FROM images i
     WHERE i.id IN (SELECT image_id FROM image_sources WHERE album_id = a.id)) AS bytes,
    (SELECT i.thumb_path
     FROM image_sources s JOIN images i ON i.id = s.image_id
     WHERE s.album_id = a.id ORDER BY s.id LIMIT 1) AS cover_thumb,
    (SELECT i.id
     FROM image_sources s JOIN images i ON i.id = s.image_id
     WHERE s.album_id = a.id ORDER BY s.id LIMIT 1) AS cover_image_id,
    (SELECT MAX(s.archived_at) FROM image_sources s WHERE s.album_id = a.id) AS last_archived_at
FROM albums a
"""


def list_albums(
    conn: sqlite3.Connection,
    *,
    year: int | None = None,
    section: str | None = None,
    kind: str | None = None,
    order: str = "oldest",
) -> list[dict[str, Any]]:
    clauses: list[str] = [
        "EXISTS (SELECT 1 FROM image_sources s0 WHERE s0.album_id = a.id)"
    ]
    params: list[Any] = []
    if year is not None:
        clauses.append("a.source_year = ?")
        params.append(year)
    if section:
        clauses.append("a.section = ?")
        params.append(section)
    if kind:
        clauses.append("a.kind = ?")
        params.append(kind)
    sql = ALBUM_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if order == "newest":
        sql += " ORDER BY COALESCE(a.source_year, 0) DESC, COALESCE(a.posted_at, '') DESC, a.id DESC"
    else:
        sql += " ORDER BY COALESCE(a.source_year, 9999) ASC, COALESCE(a.posted_at, '9999') ASC, a.id ASC"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_album(conn: sqlite3.Connection, album_id: int) -> dict[str, Any] | None:
    row = conn.execute(ALBUM_SELECT + " WHERE a.id = ?", (album_id,)).fetchone()
    return dict(row) if row else None


def count_album_images(conn: sqlite3.Connection, album_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT image_id) AS n FROM image_sources WHERE album_id = ?",
        (album_id,),
    ).fetchone()
    return int(row["n"])


def list_album_images(
    conn: sqlite3.Connection, album_id: int, limit: int = 48, offset: int = 0
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH picked AS (
            SELECT s.image_id, MIN(s.id) AS sid
            FROM image_sources s
            WHERE s.album_id = ?
            GROUP BY s.image_id
        )
        SELECT
            i.*,
            s.source_url, s.link_url, s.page_no, s.position,
            s.page_title AS source_title,
            s.archived_at AS source_archived_at,
            s.posted_at AS source_posted_at,
            p.floor, p.author, p.posted_at AS post_posted_at,
            p.source_url AS post_url,
            (SELECT COUNT(*) FROM image_sources s2 WHERE s2.image_id = i.id) AS source_count
        FROM picked
        JOIN images i ON i.id = picked.image_id
        JOIN image_sources s ON s.id = picked.sid
        LEFT JOIN posts p ON p.id = s.post_id
        ORDER BY s.id
        LIMIT ? OFFSET ?
        """,
        (album_id, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def album_image_position(conn: sqlite3.Connection, album_id: int, image_id: int) -> int:
    """图片在相册中的 1-based 顺序，用于搜索结果跳转到正确分页。"""
    row = conn.execute(
        """
        WITH picked AS (
            SELECT s.image_id, MIN(s.id) AS sid
            FROM image_sources s
            WHERE s.album_id = ?
            GROUP BY s.image_id
        ),
        ranked AS (
            SELECT image_id, ROW_NUMBER() OVER (ORDER BY sid) AS rn
            FROM picked
        )
        SELECT rn FROM ranked WHERE image_id = ?
        """,
        (album_id, image_id),
    ).fetchone()
    return int(row["rn"]) if row else 0


def list_album_export_rows(conn: sqlite3.Connection, album_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH picked AS (
            SELECT s.image_id, MIN(s.id) AS sid
            FROM image_sources s
            WHERE s.album_id = ?
            GROUP BY s.image_id
        )
        SELECT
            i.*,
            s.source_url,
            s.page_title AS source_title,
            s.archived_at AS source_archived_at,
            s.posted_at AS source_posted_at,
            p.floor, p.author, p.posted_at AS post_posted_at,
            p.source_url AS post_url,
            a.title AS album_title,
            a.source_key AS album_key
        FROM picked
        JOIN images i ON i.id = picked.image_id
        JOIN image_sources s ON s.id = picked.sid
        LEFT JOIN posts p ON p.id = s.post_id
        JOIN albums a ON a.id = s.album_id
        ORDER BY s.id
        """,
        (album_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_image(conn: sqlite3.Connection, image_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT
            i.*,
            s.source_url, s.link_url, s.page_no, s.page_title AS source_title,
            s.archived_at AS source_archived_at, s.posted_at AS source_posted_at,
            p.floor, p.author, p.posted_at AS post_posted_at, p.source_url AS post_url,
            a.id AS album_id, a.title AS album_title, a.source_year, a.kind AS album_kind,
            (SELECT COUNT(*) FROM image_sources s2 WHERE s2.image_id = i.id) AS source_count
        FROM images i
        LEFT JOIN image_sources s
            ON s.id = (SELECT MIN(id) FROM image_sources WHERE image_id = i.id)
        LEFT JOIN posts p ON p.id = s.post_id
        LEFT JOIN albums a ON a.id = s.album_id
        WHERE i.id = ?
        """,
        (image_id,),
    ).fetchone()
    return dict(row) if row else None


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_images(conn: sqlite3.Connection, query: str, limit: int = 200) -> list[dict[str, Any]]:
    like = _like(query)
    rows = conn.execute(
        """
        WITH picked AS (
            SELECT image_id, MIN(id) AS sid FROM image_sources GROUP BY image_id
        )
        SELECT
            i.*,
            s.album_id, a.title AS album_title, a.section, a.source_year, a.kind AS album_kind,
            s.source_url, s.page_title AS source_title,
            p.floor, p.author,
            (SELECT COUNT(*) FROM image_sources s2 WHERE s2.image_id = i.id) AS source_count
        FROM images i
        JOIN picked ON picked.image_id = i.id
        JOIN image_sources s ON s.id = picked.sid
        JOIN albums a ON a.id = s.album_id
        LEFT JOIN posts p ON p.id = s.post_id
        WHERE i.page_title LIKE ? ESCAPE '\\'
           OR i.caption LIKE ? ESCAPE '\\'
           OR COALESCE(i.alt_text, '') LIKE ? ESCAPE '\\'
           OR s.page_title LIKE ? ESCAPE '\\'
           OR COALESCE(p.author, '') LIKE ? ESCAPE '\\'
           OR a.title LIKE ? ESCAPE '\\'
           OR COALESCE(a.section, '') LIKE ? ESCAPE '\\'
        ORDER BY i.id DESC
        LIMIT ?
        """,
        (like, like, like, like, like, like, like, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def search_albums(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[dict[str, Any]]:
    like = _like(query)
    rows = conn.execute(
        ALBUM_SELECT
        + """
        WHERE a.title LIKE ? ESCAPE '\\'
           OR COALESCE(a.section, '') LIKE ? ESCAPE '\\'
           OR COALESCE(a.author, '') LIKE ? ESCAPE '\\'
        ORDER BY a.id DESC
        LIMIT ?
        """,
        (like, like, like, limit),
    ).fetchall()
    return [dict(row) for row in rows]
