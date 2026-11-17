from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from dramastill.config.defaults import AppSettings, app_settings_from_dict
from dramastill.paths import data_dir
from dramastill.util import now_iso


@dataclass
class Project:
    id: str
    title: str
    status: str
    stage: str
    progress: float
    created_at: str
    updated_at: str
    started_at: str | None
    elapsed_seconds: float
    output_path: str | None
    error: str | None
    settings: AppSettings
    work_dir: str


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT '',
                progress REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                output_path TEXT,
                error TEXT,
                settings_json TEXT NOT NULL,
                work_dir TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logs_project ON logs(project_id, id);
            """
        )
        self._conn.commit()

    def load_settings(self) -> AppSettings:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key='app'").fetchone()
        if row is None:
            return AppSettings()
        try:
            return app_settings_from_dict(json.loads(row["value"]))
        except (json.JSONDecodeError, TypeError):
            return AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        payload = json.dumps(settings.to_dict(), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('app', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (payload,),
            )
            self._conn.commit()

    def create_project(self, settings: AppSettings, title: str, work_dir: Path | None = None) -> Project:
        project_id = uuid.uuid4().hex[:12]
        directory = work_dir or (data_dir() / "projects" / project_id)
        directory.mkdir(parents=True, exist_ok=True)
        now = now_iso()
        payload = json.dumps(settings.to_dict(), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO projects (
                    id, title, status, stage, progress, created_at, updated_at,
                    started_at, elapsed_seconds, output_path, error, settings_json, work_dir
                ) VALUES (?, ?, 'queued', '', 0, ?, ?, NULL, 0, NULL, NULL, ?, ?)
                """,
                (project_id, title, now, now, payload, str(directory)),
            )
            self._conn.commit()
        project = self.get(project_id)
        assert project is not None
        return project

    def get(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            return None
        return self._row(row)

    def list_projects(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [self._row(row) for row in rows]

    def update(self, project_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        columns = ", ".join(f"{name}=?" for name in fields)
        values = list(fields.values()) + [project_id]
        with self._lock:
            self._conn.execute(f"UPDATE projects SET {columns} WHERE id=?", values)
            self._conn.commit()

    def claim_next_queued(self) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM projects WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = now_iso()
            cursor = self._conn.execute(
                "UPDATE projects SET status='running', updated_at=?, started_at=COALESCE(started_at, ?) "
                "WHERE id=? AND status='queued'",
                (now, now, row["id"]),
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                return None
            project_id = row["id"]
        return self.get(project_id)

    def recover_interrupted(self) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE projects
                SET status='paused',
                    updated_at=?,
                    error=CASE
                        WHEN error IS NULL OR error='' THEN 'Работа прервана. Её можно продолжить.'
                        ELSE error
                    END
                WHERE status='running'
                """,
                (now_iso(),),
            )
            self._conn.commit()
            return int(cursor.rowcount)

    def add_log(self, project_id: str, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs(project_id, ts, level, message) VALUES (?, ?, ?, ?)",
                (project_id, now_iso(), level, message[:4000]),
            )
            self._conn.commit()

    def logs(self, project_id: str, limit: int = 400) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, level, message FROM logs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM logs WHERE project_id=?", (project_id,))
            self._conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            self._conn.commit()

    def count_by_status(self, status: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM projects WHERE status=?", (status,)
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _row(row: sqlite3.Row) -> Project:
        try:
            settings = app_settings_from_dict(json.loads(row["settings_json"]))
        except (json.JSONDecodeError, TypeError):
            settings = AppSettings()
        return Project(
            id=row["id"],
            title=row["title"],
            status=row["status"],
            stage=row["stage"] or "",
            progress=float(row["progress"] or 0),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            elapsed_seconds=float(row["elapsed_seconds"] or 0),
            output_path=row["output_path"],
            error=row["error"],
            settings=settings,
            work_dir=row["work_dir"],
        )
