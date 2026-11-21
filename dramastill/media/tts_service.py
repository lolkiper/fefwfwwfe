from __future__ import annotations

import hashlib
import json
import threading
import wave
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

from dramastill.cancel import CancelToken
from dramastill.config.defaults import VoiceSettings
from dramastill.media.ffmpeg_util import FFmpegError, probe, run_ffmpeg
from dramastill.providers.base import TTSProvider
from dramastill.providers.errors import ProviderError
from dramastill.story.textutil import pack_sentences, prepare_narration, split_sentences
from dramastill.util import concat_path


def ensure_narration_audio(
    *,
    ffmpeg: str,
    ffprobe: str,
    story: dict,
    settings: VoiceSettings,
    provider: TTSProvider,
    work_dir: Path,
    token: CancelToken,
    log,
    progress,
) -> Path:
    audio_dir = work_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    master = audio_dir / "narration.wav"
    plan_path = audio_dir / "plan.json"
    fingerprint = narration_fingerprint(story, settings)
    stamp = audio_dir / "narration.stamp"
    previous = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""
    if master.exists() and _narration_reusable(ffprobe, master, stamp, fingerprint):
        if not previous:
            stamp.write_text(fingerprint, encoding="utf-8")
        log("info", "Озвучка уже собрана")
        progress(1.0)
        return master
    if master.exists():
        master.unlink()
    if previous and previous != fingerprint:
        _drop_audio_cache(audio_dir)
    plan = _load_or_build_plan(story, settings, plan_path)
    speech_dir = audio_dir / "speech"
    speech_dir.mkdir(parents=True, exist_ok=True)
    speech_items = [item for item in plan if item["kind"] == "speech"]
    _synthesize_all(
        provider=provider,
        settings=settings,
        items=speech_items,
        speech_dir=speech_dir,
        ffprobe=ffprobe,
        token=token,
        log=log,
        progress=progress,
    )
    token.check()
    log("info", "Фрагменты озвучки соединяются в одну дорожку")
    _concat_plan(ffmpeg, plan, speech_dir, audio_dir, master)
    info = probe(ffprobe, master)
    if not info.has_audio or info.duration <= 0.3:
        raise FFmpegError("Не удалось собрать голосовую дорожку")
    stamp.write_text(fingerprint, encoding="utf-8")
    progress(1.0)
    return master


def narration_fingerprint(story: dict, settings: VoiceSettings) -> str:
    payload = {
        "provider": settings.provider,
        "model": settings.model,
        "voice": settings.voice,
        "speed": round(float(settings.speed), 3),
        "emotion": settings.emotion,
        "sentence_pause_ms": int(settings.sentence_pause_ms),
        "chapter_pause_ms": int(settings.chapter_pause_ms),
        "chunk_chars": int(settings.chunk_chars),
        "chapters": [prepare_narration(chapter.get("narration") or "") for chapter in story.get("chapters") or []],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _narration_reusable(ffprobe: str, master: Path, stamp: Path, fingerprint: str) -> bool:
    try:
        info = probe(ffprobe, master)
    except FFmpegError:
        return False
    if not info.has_audio or info.duration <= 0.3:
        return False
    if not stamp.exists():
        return True
    return stamp.read_text(encoding="utf-8").strip() == fingerprint


def build_plan(story: dict, settings: VoiceSettings) -> list[dict]:
    plan: list[dict] = []
    speech_index = 0
    chapters = story.get("chapters") or []
    limit = max(400, int(settings.chunk_chars))
    for chapter_number, chapter in enumerate(chapters):
        text = prepare_narration(chapter.get("narration") or "")
        sentences = split_sentences(text)
        if settings.sentence_pause_ms > 0:
            groups = [[sentence] for sentence in sentences]
            gap = int(settings.sentence_pause_ms)
        else:
            groups = [[chunk] for chunk in pack_sentences(sentences, limit)]
            gap = 70
        for group in groups:
            plan.append(
                {
                    "kind": "speech",
                    "index": speech_index,
                    "chapter": int(chapter.get("index") or chapter_number + 1),
                    "text": " ".join(group),
                }
            )
            speech_index += 1
            if gap > 0:
                plan.append({"kind": "silence", "ms": gap})
        if chapter_number < len(chapters) - 1 and settings.chapter_pause_ms > 0:
            plan.append({"kind": "silence", "ms": int(settings.chapter_pause_ms)})
    while plan and plan[-1]["kind"] == "silence":
        plan.pop()
    if not any(item["kind"] == "speech" for item in plan):
        raise ProviderError("В сценарии нет текста для озвучки")
    return plan


def _load_or_build_plan(story: dict, settings: VoiceSettings, path: Path) -> list[dict]:
    plan = build_plan(story, settings)
    saved = _read_plan(path)
    if saved == plan:
        return saved
    if saved is not None and _speech_rows(saved) == _speech_rows(plan):
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return plan
    if saved is not None:
        _drop_audio_cache(path.parent)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def _read_plan(path: Path) -> list | None:
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(saved, list) or not saved:
        return None
    return saved


def _speech_rows(plan: list) -> list[tuple[int, str]]:
    rows = []
    for item in plan:
        if isinstance(item, dict) and item.get("kind") == "speech":
            rows.append((int(item.get("index") or 0), str(item.get("text") or "")))
    return rows


def _drop_audio_cache(audio_dir: Path) -> None:
    for folder in ("speech", "wav"):
        target = audio_dir / folder
        if not target.exists():
            continue
        for child in target.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)
    for child in audio_dir.glob("silence_*.wav"):
        child.unlink(missing_ok=True)


class ChapterAudioPrefetch:
    """Speak a finished chapter while the next one is still being written."""

    def __init__(self, *, ffmpeg: str, ffprobe: str, provider: TTSProvider, settings: VoiceSettings, work_dir: Path, token: CancelToken, log):
        del ffmpeg
        self.ffprobe = ffprobe
        self.provider = provider
        self.settings = settings
        self.work_dir = work_dir
        self.token = token
        self.log = log
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=speech_workers(settings), thread_name_prefix="drama-voice")
        self._inflight: dict[int, Future] = {}
        self._texts: dict[int, str] = {}
        self._wanted: dict[int, str] = {}
        self._announced = False

    def submit(self, story: dict) -> None:
        self.token.check()
        try:
            plan = build_plan(story, self.settings)
        except ProviderError:
            return
        speech = [item for item in plan if item.get("kind") == "speech"]
        if not speech:
            return
        speech_dir = self.work_dir / "audio" / "speech"
        speech_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if not self._announced:
                self._announced = True
                self.log("info", "Готовые главы озвучиваются, пока пишутся следующие")
        queued = 0
        for item in speech:
            index = int(item["index"])
            text = str(item["text"])
            digest = _speech_digest(text)
            dest = speech_dir / f"{index:05d}.mp3"
            with self._lock:
                self._texts[index] = text
                self._wanted[index] = digest
                current = self._inflight.get(index)
                busy = current is not None and not current.done()
            if busy:
                continue
            if _speech_matches(self.ffprobe, dest, text):
                continue
            dest.unlink(missing_ok=True)
            dest.with_suffix(".txt").unlink(missing_ok=True)
            with self._lock:
                current = self._inflight.get(index)
                if current is not None and not current.done():
                    continue
                self._inflight[index] = self._pool.submit(self._speak, index, dest)
                queued += 1
        if queued:
            self.log("info", f"В озвучку добавлено фрагментов: {queued}")

    def wait(self) -> None:
        with self._lock:
            pending = dict(self._inflight)
        total = len(pending)
        if total:
            self.log("info", f"Жду окончания озвучки: {total} фрагментов")
        done = 0
        error: Exception | None = None
        while pending:
            finished, _waiting = wait(list(pending.values()), timeout=25, return_when=FIRST_COMPLETED)
            if not finished:
                self.token.check()
                self.log("info", f"Озвучка ещё идёт: готово {done} из {total}")
                continue
            for future in finished:
                index = next(key for key, item in pending.items() if item is future)
                pending.pop(index, None)
                try:
                    future.result()
                except Exception as exc:
                    error = exc
                    self.log("warning", f"Озвучка фрагмента {index + 1} не удалась: {exc}")
                else:
                    done += 1
                    self.log("info", f"Озвучка фрагмента {index + 1} готова, {done} из {total}")
        self._pool.shutdown(wait=True)
        if total:
            self.log("info", "Озвучка фрагментов закончена")
        if error is not None:
            raise error

    def _speak(self, index: int, dest: Path) -> None:
        while True:
            self.token.check()
            with self._lock:
                text = self._texts[index]
                digest = self._wanted[index]
            self.log("info", f"Озвучка фрагмента {index + 1} начата")
            partial = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
            if partial.exists():
                partial.unlink()
            self.provider.synthesize(
                text=text,
                dest=partial,
                voice=self.settings.voice,
                speed=float(self.settings.speed),
                emotion=self.settings.emotion,
                language="",
            )
            if not _audio_ok(self.ffprobe, partial):
                partial.unlink(missing_ok=True)
                raise ProviderError(f"Пустой аудиофрагмент {index + 1}")
            with self._lock:
                if self._wanted.get(index) != digest:
                    partial.unlink(missing_ok=True)
                    self.log("info", f"Озвучка фрагмента {index + 1} устарела после правки главы, повторяю")
                    continue
                partial.replace(dest)
                dest.with_suffix(".txt").write_text(digest, encoding="utf-8")
                return


def speech_workers(settings: VoiceSettings) -> int:
    chosen = int(settings.parallelism or 4)
    return max(1, min(4, chosen))


def chapter_time_spans(plan: list[dict], speech_dir: Path, ffprobe: str) -> list[tuple[float, float, int]]:
    """Начало и конец каждой главы на общей звуковой дорожке."""
    clock = 0.0
    bounds: dict[int, list[float]] = {}
    order: list[int] = []
    for item in plan:
        if item.get("kind") == "speech":
            chapter = int(item.get("chapter") or 1)
            path = speech_dir / f"{int(item.get('index') or 0):05d}.mp3"
            duration = 0.0
            if path.exists():
                try:
                    duration = max(0.0, probe(ffprobe, path).duration)
                except FFmpegError:
                    duration = 0.0
            if chapter not in bounds:
                bounds[chapter] = [clock, clock]
                order.append(chapter)
            clock += duration
            bounds[chapter][1] = clock
        elif item.get("kind") == "silence":
            clock += max(0, int(item.get("ms") or 0)) / 1000.0
            if order:
                bounds[order[-1]][1] = clock
    return [(bounds[chapter][0], bounds[chapter][1], chapter) for chapter in order]


def _synthesize_all(*, provider, settings, items, speech_dir, ffprobe, token, log, progress) -> None:
    pending = []
    for item in items:
        dest = speech_dir / f"{item['index']:05d}.mp3"
        if _speech_matches(ffprobe, dest, str(item["text"])):
            continue
        pending.append(item)
    done = len(items) - len(pending)
    if not pending:
        progress(1.0)
        return
    workers = speech_workers(settings)
    log("info", f"Озвучка: {len(pending)} фрагментов, потоков: {workers}")

    def task(item: dict) -> int:
        token.check()
        dest = speech_dir / f"{item['index']:05d}.mp3"
        partial = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
        if partial.exists():
            partial.unlink()
        provider.synthesize(
            text=item["text"],
            dest=partial,
            voice=settings.voice,
            speed=float(settings.speed),
            emotion=settings.emotion,
            language="",
        )
        if not _audio_ok(ffprobe, partial):
            partial.unlink(missing_ok=True)
            raise ProviderError(f"Пустой аудиофрагмент {item['index'] + 1}")
        partial.replace(dest)
        dest.with_suffix(".txt").write_text(_speech_digest(str(item["text"])), encoding="utf-8")
        return item["index"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(task, item) for item in pending]
        for future in as_completed(futures):
            token.check()
            future.result()
            done += 1
            progress(done / len(items))


def _speech_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _speech_matches(ffprobe: str, dest: Path, text: str) -> bool:
    stamp = dest.with_suffix(".txt")
    if not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != _speech_digest(text):
        return False
    return _audio_ok(ffprobe, dest)


def _audio_ok(ffprobe: str, path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 300:
        return False
    try:
        info = probe(ffprobe, path)
    except FFmpegError:
        return False
    return info.has_audio and info.duration > 0.02


def _concat_plan(ffmpeg: str, plan: list[dict], speech_dir: Path, audio_dir: Path, master: Path) -> None:
    wav_dir = audio_dir / "wav"
    wav_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for item in plan:
        if item["kind"] == "speech":
            source = speech_dir / f"{item['index']:05d}.mp3"
            wav = wav_dir / f"{item['index']:05d}.wav"
            if not wav.exists():
                run_ffmpeg(
                    [
                        ffmpeg,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(source),
                        "-ar",
                        "48000",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_s16le",
                        str(wav),
                    ]
                )
            lines.append(f"file '{concat_path(wav)}'")
        else:
            silence = audio_dir / f"silence_{int(item['ms'])}.wav"
            _write_silence(silence, int(item["ms"]))
            lines.append(f"file '{concat_path(silence)}'")
    listing = audio_dir / "concat.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if master.exists():
        master.unlink()
    run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(master),
        ]
    )


def _write_silence(path: Path, milliseconds: int) -> None:
    if path.exists() and path.stat().st_size > 40:
        return
    frames = int(48000 * max(0, milliseconds) / 1000)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * frames)
