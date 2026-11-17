from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from dramastill.paths import font_path


def compose_thumbnail(image_path: Path, text: str, dest: Path) -> Path:
    """Превью собирается из единственного кадра проекта, без новой генерации."""
    source = Image.open(image_path).convert("RGB")
    canvas = _cover(source, 1280, 720)
    array = np.asarray(canvas).astype(np.float32)
    height = array.shape[0]
    gradient = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    mask = np.clip((gradient - 0.42) / 0.58, 0, 1)
    array *= 1.0 - 0.75 * mask
    array[: int(height * 0.18)] *= 0.82
    graded = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(graded)
    font_file = font_path()
    phrase = " ".join((text or "ИСТОРИЯ").split())
    font, lines = _fit_text(draw, phrase, font_file, 1120)
    line_height = font.size + 8
    block = line_height * len(lines)
    y = 720 - block - 54
    for line in lines:
        width = draw.textlength(line, font=font)
        x = (1280 - width) / 2
        draw.text((x, y), line, font=font, fill=(255, 248, 236), stroke_width=6, stroke_fill=(12, 8, 6))
        y += line_height
    dest.parent.mkdir(parents=True, exist_ok=True)
    graded.save(dest, "JPEG", quality=92, optimize=True)
    return dest


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_file: Path | None, max_width: int):
    size = 86
    while size >= 36:
        font = _load_font(font_file, size)
        lines = _wrap(draw, text, font, max_width)
        if len(lines) <= 3 and all(draw.textlength(line, font=font) <= max_width for line in lines):
            return font, lines
        size -= 4
    font = _load_font(font_file, 36)
    return font, _wrap(draw, text, font, max_width)[:3]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return ["ИСТОРИЯ"]
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or ["ИСТОРИЯ"]


def _load_font(font_file: Path | None, size: int) -> ImageFont.ImageFont:
    if font_file is not None:
        return ImageFont.truetype(str(font_file), size=size)
    return ImageFont.load_default()
