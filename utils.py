"""界面与导出共用的格式化工具。"""
from __future__ import annotations

import re
from datetime import datetime


def safe_filename(value: str, fallback: str = "image", limit: int = 90) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "")
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return (value or fallback)[:limit]


def format_datetime(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not value:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime(fmt)
    except ValueError:
        return text[:16]


def format_date(value) -> str:
    return format_datetime(value, "%Y-%m-%d")


def human_size(value) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
