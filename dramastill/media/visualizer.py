from __future__ import annotations

import numpy as np


def parse_color(value: str, opacity: float) -> tuple[int, int, int]:
    text = (value or "#E6C36A").strip().lstrip("#")
    if len(text) != 6:
        text = "E6C36A"
    red = int(text[0:2], 16)
    green = int(text[2:4], 16)
    blue = int(text[4:6], 16)
    factor = min(1.0, max(0.05, float(opacity)))
    return (
        max(0, min(255, int(round(red * factor)))),
        max(0, min(255, int(round(green * factor)))),
        max(0, min(255, int(round(blue * factor)))),
    )


class BarRenderer:
    """Полоски рисуются из уже посчитанной громкости, без случайной анимации."""

    def __init__(self, width: int, height: int, color: tuple[int, int, int], gap: float):
        self.width = width
        self.height = height
        self.color = np.array(color, dtype=np.float32)
        self.gap = min(0.75, max(0.05, float(gap)))
        gradient = np.linspace(0.42, 1.0, height, dtype=np.float32)[:, None]
        rgb = (gradient * self.color).astype(np.uint8)
        self.column = np.zeros((height, 4), dtype=np.uint8)
        self.column[:, :3] = rgb
        self.column[:, 3] = 255

    def frame(self, levels: np.ndarray) -> np.ndarray:
        image = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        count = max(1, int(levels.shape[0]))
        margin = int(self.width * 0.035)
        usable = max(count, self.width - margin * 2)
        slot = usable / count
        bar_width = max(1, int(slot * (1.0 - self.gap)))
        for index in range(count):
            level = float(levels[index])
            if level <= 0.01:
                continue
            bar_height = max(1, int(round(min(1.0, level) * (self.height - 2))))
            x0 = margin + int(index * slot + (slot - bar_width) / 2)
            x1 = min(self.width, x0 + bar_width)
            if x1 <= x0:
                continue
            y0 = self.height - bar_height
            image[y0 : self.height, x0:x1] = self.column[y0 : self.height][:, None, :]
        return image

    def frames(self, levels: np.ndarray):
        for row in levels:
            yield self.frame(row).tobytes()
