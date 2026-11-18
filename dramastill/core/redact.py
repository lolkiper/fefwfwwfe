from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.=]{6,}", re.IGNORECASE),
    re.compile(r"(xi-api-key['\"]?\s*[:=]\s*)\S+", re.IGNORECASE),
    re.compile(r"(api[_-]?key['\"]?\s*[:=]\s*)\S+", re.IGNORECASE),
]


def redact(text: str | None, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    if not text:
        return ""
    cleaned = str(text)
    for secret in secrets or ():
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "***")
    for pattern in _PATTERNS:
        cleaned = pattern.sub(
            lambda match: match.group(1) + "***" if match.lastindex else "***",
            cleaned,
        )
    return cleaned
