"""联商影像档案：全站相册浏览 Web 应用。"""
from __future__ import annotations

import csv
import io
import math
import os
import tempfile
import zipfile
from pathlib import Path

from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

import db
from utils import format_date, format_datetime, human_size, safe_filename


def create_app(data_dir: str | Path | None = None) -> Flask:
    base_dir = Path(__file__).resolve().parent
    resolved = Path(
        data_dir or os.environ.get("LINKSHOP_DATA_DIR") or (base_dir / "data")
    ).expanduser().resolve()

    app = Flask(__name__)
    app.config.update(
        DATA_DIR=resolved,
        DB_PATH=resolved / "archive.db",
        PER_PAGE=48,
    )
    resolved.mkdir(parents=True, exist_ok=True)
    db.init_db(app.config["DB_PATH"]).close()

    app.jinja_env.filters["filesize"] = human_size
    app.jinja_env.filters["dt"] = format_datetime
    app.jinja_env.filters["date"] = format_date

    @app.context_processor
    def inject_url_with():
        def url_with(**overrides):
            args = request.args.to_dict(flat=True)
            for key, value in overrides.items():
                if value is None or value == "":
                    args.pop(key, None)
                else:
                    args[key] = str(value)
            return url_for("index", **args)

        return {"url_with": url_with}

    def get_conn():
        if "db" not in g:
            g.db = db.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_conn(exception=None):  # noqa: ARG001
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.get("/")
    def index():
        conn = get_conn()
        year = request.args.get("year", type=int)
        section = request.args.get("section") or None
        kind = request.args.get("kind") or None
        order = request.args.get("order") or "oldest"
        if order not in ("oldest", "newest"):
            order = "oldest"
        albums = db.list_albums(conn, year=year, section=section, kind=kind, order=order)
        stats = db.get_stats(conn)
        years = db.list_years(conn)
        sections = db.list_sections(conn)
        return render_template(
            "index.html",
            albums=albums,
            stats=stats,
            years=years,
            sections=sections,
            active_year=year,
            active_section=section,
            active_kind=kind,
            active_order=order,
        )

    @app.get("/album/<int:album_id>")
    def album_detail(album_id: int):
        conn = get_conn()
        album = db.get_album(conn, album_id)
        if not album or album["image_count"] == 0:
            abort(404)
        per_page = app.config["PER_PAGE"]
        total = album["image_count"]
        pages = max(1, math.ceil(total / per_page))
        page = request.args.get("page", 1, type=int)
        page = min(max(1, page), pages)
        offset = (page - 1) * per_page
        images = db.list_album_images(conn, album_id, limit=per_page, offset=offset)
        page_numbers = list(range(max(1, page - 2), min(pages, page + 2) + 1))
        return render_template(
            "album.html",
            album=album,
            images=images,
            page=page,
            pages=pages,
            total=total,
            offset=offset,
            page_numbers=page_numbers,
        )

    @app.get("/album/<int:album_id>/image/<int:image_id>")
    def album_image(album_id: int, image_id: int):
        conn = get_conn()
        album = db.get_album(conn, album_id)
        if not album:
            abort(404)
        position = db.album_image_position(conn, album_id, image_id)
        if position <= 0:
            abort(404)
        page = max(1, math.ceil(position / app.config["PER_PAGE"]))
        return redirect(
            url_for("album_detail", album_id=album_id, page=page) + f"#image-{image_id}"
        )

    @app.get("/search")
    def search():
        conn = get_conn()
        query = (request.args.get("q") or "").strip()
        albums = db.search_albums(conn, query) if query else []
        images = db.search_images(conn, query) if query else []
        stats = db.get_stats(conn)
        return render_template(
            "search.html",
            query=query,
            albums=albums,
            images=images,
            stats=stats,
        )

    @app.get("/runs")
    def runs():
        conn = get_conn()
        rows = conn.execute(
            """
            SELECT * FROM site_crawls
            ORDER BY id DESC LIMIT 30
            """
        ).fetchall()
        queue = db.queue_stats(conn)
        stats = db.get_stats(conn)
        return render_template(
            "runs.html",
            runs=[dict(row) for row in rows],
            queue=queue,
            stats=stats,
        )

    @app.get("/media/<path:rel_path>")
    def media(rel_path: str):
        if not (rel_path.startswith("images/") or rel_path.startswith("thumbs/")):
            abort(404)
        response = send_from_directory(app.config["DATA_DIR"], rel_path, conditional=True)
        response.cache_control.public = True
        response.cache_control.max_age = 31536000
        response.cache_control.immutable = True
        return response

    @app.get("/download/image/<int:image_id>")
    def download_image(image_id: int):
        conn = get_conn()
        image = db.get_image(conn, image_id)
        if not image:
            abort(404)
        path = app.config["DATA_DIR"] / image["rel_path"]
        if not path.is_file():
            abort(404)
        title = safe_filename(image["source_title"] or image["page_title"], limit=55)
        floor = safe_filename(image["floor"] or "", limit=12)
        stamp = (image["source_archived_at"] or image["archived_at"] or "")[:10].replace("-", "")
        pieces = [piece for piece in (title, floor, stamp) if piece]
        download_name = "_".join(pieces) + f".{image['ext']}"
        return send_file(
            path,
            as_attachment=True,
            download_name=download_name,
            mimetype=image["mime"] or "application/octet-stream",
        )

    @app.get("/download/album/<int:album_id>")
    def download_album(album_id: int):
        conn = get_conn()
        album = db.get_album(conn, album_id)
        if not album:
            abort(404)
        rows = db.list_album_export_rows(conn, album_id)
        if not rows:
            abort(404)

        temp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        manifest = io.StringIO(newline="")
        writer = csv.writer(manifest)
        writer.writerow(
            [
                "序号",
                "文件名",
                "页面标题",
                "栏目",
                "类型",
                "楼层",
                "作者",
                "原帖时间",
                "存档时间",
                "宽",
                "高",
                "字节",
                "SHA256",
                "原图地址",
                "原帖地址",
            ]
        )

        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, row in enumerate(rows, start=1):
                source = app.config["DATA_DIR"] / row["rel_path"]
                floor = safe_filename(row["floor"] or "", limit=12)
                title = safe_filename(row["source_title"] or album["title"], limit=45)
                filename = f"{index:03d}_{title}_{floor}.{row['ext']}"
                arcname = f"images/{filename}"
                exists = source.is_file()
                if exists:
                    archive.write(source, arcname, compress_type=zipfile.ZIP_STORED)
                writer.writerow(
                    [
                        index,
                        filename if exists else "（文件缺失）",
                        row["source_title"] or row["page_title"],
                        album["section"] or "",
                        "论坛帖" if album["kind"] == "thread" else "文章",
                        row["floor"] or "",
                        row["author"] or "",
                        row["post_posted_at"] or row["source_posted_at"] or row["posted_at"] or "",
                        row["source_archived_at"] or row["archived_at"],
                        row["width"],
                        row["height"],
                        row["bytes"],
                        row["sha256"],
                        row["source_url"],
                        row["post_url"] or album["url"],
                    ]
                )

            readme = (
                f"联商影像档案 / {album['title']}\n"
                f"原页：{album['url']}\n"
                f"栏目：{album['section'] or '未分类'}\n"
                f"图像：{len(rows)} 张\n"
                f"导出时间：{db.utcnow()}\n\n"
                "说明：\n"
                "1. 文件名以序号、页面标题、楼层/位置组成，便于脱离数据库后仍能识别来源。\n"
                "2. 内容相同的图片在档案库中只保存一份，本相册展示的是该相册内的来源记录。\n"
                "3. 图片版权归原作者及原网站所有，仅供个人检索、存档与研究使用。\n"
            )
            archive.writestr("README.txt", readme.encode("utf-8"))
            archive.writestr("manifest.csv", manifest.getvalue().encode("utf-8-sig"))

        temp.seek(0)
        download_name = f"{safe_filename(album['title'], limit=55)}_相册.zip"
        return send_file(
            temp,
            mimetype="application/zip",
            as_attachment=True,
            download_name=download_name,
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "data_dir": str(app.config["DATA_DIR"])}

    @app.errorhandler(404)
    def not_found(error):  # noqa: ARG001
        return render_template("404.html"), 404

    return app


if __name__ == "__main__":
    import argparse

    arg = argparse.ArgumentParser(description="联商影像档案 Web 应用")
    arg.add_argument("--data-dir", default="data")
    arg.add_argument("--host", default="127.0.0.1")
    arg.add_argument("--port", type=int, default=5055)
    arg.add_argument("--debug", action="store_true")
    args = arg.parse_args()
    application = create_app(args.data_dir)
    application.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=False,
    )
