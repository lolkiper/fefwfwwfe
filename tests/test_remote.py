import json
import time
import urllib.request
from pathlib import Path

from dramastill.config.defaults import AppSettings
from dramastill.core.database import Database
from dramastill.core.secrets import SecretStore
from dramastill.media.tts_service import build_plan
from dramastill.pipeline.runner import Callbacks
from dramastill.remote import WorkerStatus, assign_jobs, choose_worker
from dramastill.remote.client import run_on_worker
from dramastill.remote.server import start_worker
from dramastill.cancel import CancelToken


def test_chapter_plan_keeps_indexes_when_only_the_prefix_is_ready():
    settings = AppSettings().voice
    story = {
        "chapters": [
            {"narration": "Первая глава закончилась. Вторая фраза тоже на месте."},
            {"narration": "Вторая глава начинается после паузы и не теряет текст."},
        ]
    }
    full = [item["index"] for item in build_plan(story, settings) if item["kind"] == "speech"]
    partial = [item["index"] for item in build_plan({"chapters": story["chapters"][:1]}, settings) if item["kind"] == "speech"]
    assert partial == full[: len(partial)]
    assert full[0] == 0


def test_dispatcher_skips_a_hot_server_and_spreads_twenty_jobs():
    cool = [
        WorkerStatus("http://a", ok=True, busy=0, temp_c=60, accept=True),
        WorkerStatus("http://b", ok=True, busy=2, temp_c=70, accept=True),
        WorkerStatus("http://hot", ok=True, busy=0, temp_c=90, accept=True),
        WorkerStatus("http://down", ok=False, accept=False),
    ]
    assert choose_worker(cool, temp_limit=82) == "http://a"
    assigned = assign_jobs([f"job-{index}" for index in range(20)], cool, temp_limit=82)
    assert len(assigned) == 20
    assert "http://hot" not in assigned.values()
    assert "http://down" not in assigned.values()
    counts = {url: list(assigned.values()).count(url) for url in ("http://a", "http://b")}
    assert counts["http://a"] == 11
    assert counts["http://b"] == 9


def test_worker_finishes_after_the_client_request_returns(tmp_path):
    started = time.perf_counter()

    def executor(payload: dict, job_dir: Path) -> None:
        time.sleep(0.6)
        (job_dir / "output.mp4").write_bytes(b"mp4")
        (job_dir / "sidecar.jpg").write_bytes(b"jpg")
        (job_dir / "sidecar.youtube.json").write_text("{}", encoding="utf-8")
        (job_dir / "payload-id.txt").write_text(payload["id"], encoding="utf-8")

    server = start_worker(tmp_path / "jobs", token="secret", executor=executor, capacity=1)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    body = json.dumps({"id": "job1", "title": "Ролик"}).encode("utf-8")
    request = urllib.request.Request(
        url + "/v1/jobs",
        data=body,
        headers={"Content-Type": "application/json", "X-DramaStill-Token": "secret"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=5) as response:
        accepted = json.loads(response.read().decode("utf-8"))
    assert accepted["continues_without_client"] is True
    assert time.perf_counter() - started < 0.5
    deadline = time.perf_counter() + 5
    status = {}
    while time.perf_counter() < deadline:
        with urllib.request.urlopen(
            urllib.request.Request(url + "/v1/jobs/job1", headers={"X-DramaStill-Token": "secret"})
        ) as response:
            status = json.loads(response.read().decode("utf-8"))
        if status.get("status") == "completed":
            break
        time.sleep(0.1)
    assert status.get("status") == "completed"
    with urllib.request.urlopen(
        urllib.request.Request(url + "/v1/jobs/job1/output.mp4", headers={"X-DramaStill-Token": "secret"})
    ) as response:
        assert response.read() == b"mp4"
    busy = json.dumps({"id": "job2"}).encode("utf-8")
    # The first job has finished, so a second one is accepted.
    second = urllib.request.Request(
        url + "/v1/jobs",
        data=busy,
        headers={"Content-Type": "application/json", "X-DramaStill-Token": "secret"},
        method="POST",
    )
    with urllib.request.urlopen(second, timeout=5) as response:
        assert response.status == 202
    server.shutdown()


def test_desktop_downloads_the_finished_file(tmp_path):
    def executor(payload: dict, job_dir: Path) -> None:
        (job_dir / "output.mp4").write_bytes(b"video-bytes")
        (job_dir / "sidecar.jpg").write_bytes(b"jpeg")
        (job_dir / "sidecar.youtube.json").write_text('{"title":"ok"}', encoding="utf-8")

    server = start_worker(tmp_path / "worker", token="secret", executor=executor)
    port = server.server_address[1]
    settings = AppSettings()
    settings.video.worker_urls = f"http://127.0.0.1:{port}"
    settings.video.worker_token = "secret"
    settings.video.output_dir = str(tmp_path / "out")
    db = Database(tmp_path / "app.sqlite3")
    project = db.create_project(settings, "Серверный ролик", tmp_path / "project")
    db.update(project.id, status="running")
    logs: list[str] = []
    outcome = run_on_worker(
        db,
        project.id,
        SecretStore(tmp_path / "secrets"),
        CancelToken(),
        Callbacks(log=lambda _level, message: logs.append(message), progress=lambda *_: None, title=lambda *_: None),
    )
    assert outcome == "done"
    saved = db.get(project.id)
    assert saved is not None
    assert saved.status == "completed"
    assert saved.output_path
    assert Path(saved.output_path).read_bytes() == b"video-bytes"
    assert any("сервер" in line.lower() for line in logs)
    server.shutdown()
