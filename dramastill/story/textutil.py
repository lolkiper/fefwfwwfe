from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Модель не вернула JSON")
    blob = raw[start : end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", blob)
        data = json.loads(repaired)
    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом")
    return data


def split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized:
        return []
    parts = re.split(r"(?<=[\.\!\?\u2026])\s+", normalized)
    return [part.strip() for part in parts if part.strip()]


def pack_sentences(sentences: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        if not buffer:
            buffer = sentence
            continue
        if len(buffer) + 1 + len(sentence) <= limit:
            buffer = f"{buffer} {sentence}"
            continue
        chunks.append(buffer)
        buffer = sentence
    if buffer:
        chunks.append(buffer)
    flattened: list[str] = []
    for chunk in chunks:
        flattened.extend(_hard_split(chunk, limit))
    return [item for item in flattened if item.strip()]


def _hard_split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces = re.split(r"(?<=[,;:—–-])\s+", text)
    if len(pieces) == 1:
        return [text[index : index + limit].strip() for index in range(0, len(text), limit)]
    return pack_sentences(pieces, limit)


def prepare_narration(text: str) -> str:
    cleaned = text.replace("\u00a0", " ")
    cleaned = re.sub(r"[*_#`>]+", "", cleaned)
    cleaned = re.sub(r"\[(.*?)\]", r"\1", cleaned)
    cleaned = re.sub(r"\((?:пауза|вздох|музыка|смех|плач)\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
