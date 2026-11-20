from __future__ import annotations

import json
import re
from typing import Any


def extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip().lstrip("\ufeff")
    raw = raw.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Модель не вернула JSON")
    blob = raw[start : end + 1]
    data = _load_object(blob)
    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом")
    return data


def _load_object(blob: str) -> Any:
    variants = [blob, _escape_string_breaks(blob)]
    last: json.JSONDecodeError | None = None
    for item in variants:
        cleaned = re.sub(r",\s*([}\]])", r"\1", item)
        for candidate in (item, cleaned):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                last = exc
    assert last is not None
    raise last


def _escape_string_breaks(blob: str) -> str:
    out: list[str] = []
    in_string = False
    escape = False
    for char in blob:
        if escape:
            out.append(char)
            escape = False
            continue
        if char == "\\" and in_string:
            out.append(char)
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            out.append(char)
            continue
        if in_string and char == "\n":
            out.append("\\n")
            continue
        if in_string and char == "\r":
            continue
        out.append(char)
    return "".join(out)


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
