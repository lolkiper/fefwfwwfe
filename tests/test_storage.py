from datetime import datetime, timedelta, timezone

from dramastill.config.defaults import AppSettings
from dramastill.core.database import Database
from dramastill.core.redact import redact
from dramastill.core.secrets import SecretStore


def test_project_roundtrip_and_resume_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAMASTILL_DATA", str(tmp_path / "data"))
    db = Database(tmp_path / "app.sqlite3")
    settings = AppSettings()
    settings.story.idea = "идея"
    project = db.create_project(settings, "История")
    assert project.status == "queued"
    claimed = db.claim_next_queued()
    assert claimed is not None and claimed.id == project.id
    assert db.claim_next_queued() is None
    db.update(claimed.id, status="running", progress=0.4, stage="tts")
    assert db.recover_interrupted() == 1
    restored = db.get(project.id)
    assert restored.status == "paused"
    assert restored.settings.story.idea == "идея"
    db.add_log(project.id, "info", "шаг")
    assert db.logs(project.id)[0]["message"] == "шаг"


def test_interrupted_work_keeps_the_time_already_spent(tmp_path):
    db = Database(tmp_path / "app.sqlite3")
    project = db.create_project(AppSettings(), "История")
    db.claim_next_queued()
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    db.update(project.id, started_at=started)
    assert db.recover_interrupted() == 1
    restored = db.get(project.id)
    assert restored is not None
    assert restored.status == "paused"
    assert restored.started_at is None
    assert restored.elapsed_seconds > 7000


def test_secrets_are_encrypted_and_logs_are_redacted(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    secret = "sk-testsecretvalue123456"
    store.set("llm", secret)
    blob = (tmp_path / "secrets" / "secrets.bin").read_bytes()
    assert secret.encode() not in blob
    assert store.get("llm") == secret
    store.set("llm", "")
    assert store.get("llm") == ""
    assert secret not in redact(f"Ошибка ключа {secret} и Bearer abcdefghijklmnop", [secret])
