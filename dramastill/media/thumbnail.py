from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from dramastill.paths import font_path


YELLOW = (255, 214, 0)
WHITE = (255, 255, 255)
RED = (255, 38, 32)
STROKE = (0, 0, 0)
_TONES = {"yellow": YELLOW, "white": WHITE, "red": RED}


def compose_thumbnail(image_path: Path, text: str, dest: Path, lines: list | None = None) -> Path:
    """Превью 1280×720: кадр истории и плакат из жёлтых, белых и красной строк."""
    source = Image.open(image_path).convert("RGB")
    canvas = _cover(source, 1280, 720)
    array = np.asarray(canvas).astype(np.float32)
    height = array.shape[0]
    gradient = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    mask = np.clip((gradient - 0.34) / 0.66, 0, 1)
    array *= 1.0 - 0.5 * mask
    graded = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(graded)
    rows = poster_lines(text, lines)
    font, gap = _fit_block(draw, rows, font_path(), 1180, 430)
    line_height = font.size + gap
    block = line_height * len(rows) - gap
    y = 720 - block - 28
    stroke = max(6, font.size // 12)
    for phrase, tone in rows:
        width = draw.textlength(phrase, font=font)
        x = (1280 - width) / 2
        if tone == "red":
            pad_x, pad_y = 16, 4
            draw.rectangle((x - pad_x, y - pad_y, x + width + pad_x, y + font.size + pad_y + 2), fill=STROKE)
        draw.text((x, y), phrase, font=font, fill=_TONES[tone], stroke_width=stroke, stroke_fill=STROKE)
        y += line_height
    dest.parent.mkdir(parents=True, exist_ok=True)
    graded.save(dest, "JPEG", quality=92, optimize=True)
    return dest


def poster_lines(text: str, lines: list | None = None) -> list[tuple[str, str]]:
    parsed = _parse_lines(lines)
    if len(parsed) >= 3:
        return _with_tones(parsed)[:5]
    phrase = " ".join(str(text or "").replace("|", " ").split())
    words = phrase.split() or ["ИСТОРИЯ"]
    packed = _pack(words, 4 if len(words) >= 4 else max(1, len(words)))
    return _with_tones([(line, "") for line in packed])


def _parse_lines(lines: list | None) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    if not isinstance(lines, list):
        return parsed
    for item in lines:
        if isinstance(item, str):
            phrase, tone = item, ""
        elif isinstance(item, dict):
            phrase = str(item.get("text") or "")
            tone = str(item.get("tone") or item.get("color") or "").strip().lower()
        else:
            continue
        phrase = " ".join(phrase.split())
        if not phrase:
            continue
        if tone not in _TONES:
            tone = ""
        parsed.append((phrase[:48], tone))
    return parsed


def _with_tones(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    count = len(rows)
    painted: list[tuple[str, str]] = []
    for index, (phrase, tone) in enumerate(rows):
        if tone in _TONES:
            painted.append((phrase, tone))
            continue
        if count == 1 or index == count - 1:
            painted.append((phrase, "red"))
        elif index == 0 or (count >= 5 and index == count - 2):
            painted.append((phrase, "yellow"))
        else:
            painted.append((phrase, "white"))
    if painted and all(tone != "red" for _phrase, tone in painted):
        phrase, _tone = painted[-1]
        painted[-1] = (phrase, "red")
    if len(painted) > 1 and all(tone != "yellow" for _phrase, tone in painted[:-1]):
        phrase, _tone = painted[0]
        painted[0] = (phrase, "yellow")
    return painted


def _pack(words: list[str], count: int) -> list[str]:
    count = max(1, min(count, len(words)))
    total = sum(len(word) for word in words) + len(words) - 1
    target = total / count
    groups: list[list[str]] = []
    current: list[str] = []
    length = 0
    for word in words:
        extra = len(word) if not current else len(word) + 1
        if current and length + extra > target and len(groups) < count - 1:
            groups.append(current)
            current = [word]
            length = len(word)
        else:
            current.append(word)
            length += extra
    if current:
        groups.append(current)
    while len(groups) > count:
        groups[-2].extend(groups[-1])
        groups.pop()
    return [" ".join(group) for group in groups if group]


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _fit_block(draw: ImageDraw.ImageDraw, rows: list[tuple[str, str]], font_file: Path | None, max_width: int, max_block: int):
    size = 108
    while size >= 42:
        font = _load_font(font_file, size)
        gap = max(0, size // 18)
        widths = [draw.textlength(phrase, font=font) for phrase, _tone in rows]
        if max(widths) <= max_width and (size + gap) * len(rows) <= max_block:
            return font, gap
        size -= 4
    font = _load_font(font_file, 42)
    return font, 2


def _load_font(font_file: Path | None, size: int) -> ImageFont.ImageFont:
    if font_file is not None:
        return ImageFont.truetype(str(font_file), size=size)
    return ImageFont.load_default()
