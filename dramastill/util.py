from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_display_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


def even(value: float) -> int:
    number = int(round(value))
    if number % 2:
        number -= 1
    return max(2, number)


def safe_filename(title: str, suffix: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\n\r\t]', " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "video"
    return cleaned[:80] + suffix


def concat_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "'\\''")


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))
