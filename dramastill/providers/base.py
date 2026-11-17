from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    finish_reason: str = "stop"


@dataclass(frozen=True)
class VoiceInfo:
    id: str
    label: str
    gender: str = ""
    language: str = ""


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> LLMResult: ...


class ImageProvider(Protocol):
    def generate(self, *, prompt: str, dest: Path, size: str, seed: int | None) -> None: ...


class TTSProvider(Protocol):
    name: str

    def synthesize(
        self,
        *,
        text: str,
        dest: Path,
        voice: str,
        speed: float,
        emotion: str,
        language: str,
    ) -> None: ...

    def list_voices(self, language: str | None = None) -> list[VoiceInfo]: ...
