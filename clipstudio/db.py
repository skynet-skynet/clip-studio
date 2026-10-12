"""SQLite-хранилище: видео, реестр прав, задачи, кандидаты, клипы, публикации.

Одна база на проект (data/clipstudio.db). Доступ потокобезопасный через общий lock —
приложение лёгкое и однопользовательское, этого достаточно.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

JSON_COLUMNS = {"params", "log", "manual", "scores", "tags", "candidate_ids", "review", "words"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    title TEXT DEFAULT '',
    origin_url TEXT DEFAULT '',
    local_path TEXT DEFAULT '',
    subs_path TEXT DEFAULT '',
    duration REAL DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    license TEXT DEFAULT '',
    channel TEXT DEFAULT '',
    rights_status TEXT DEFAULT 'pending',
    rights_reason TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',
    status_message TEXT DEFAULT '',
    transcript_path TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS rights_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER,
    source_id TEXT,
    origin_url TEXT,
    license TEXT,
    proof TEXT,
    decision TEXT,
    reason TEXT,
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT,
    params TEXT,
    status TEXT DEFAULT 'queued',
    progress REAL DEFAULT 0,
    stage TEXT DEFAULT '',
    log TEXT DEFAULT '[]',
    error TEXT DEFAULT '',
    manual TEXT DEFAULT '{}',
    cost_usd REAL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    video_id INTEGER,
    start REAL,
    "end" REAL,
    quote TEXT DEFAULT '',
    why TEXT DEFAULT '',
    scores TEXT DEFAULT '{}',
    total REAL DEFAULT 0,
    title TEXT DEFAULT '',
    hook TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    risk TEXT DEFAULT '',
    origin TEXT DEFAULT 'llm',
    status TEXT DEFAULT 'proposed',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    candidate_ids TEXT DEFAULT '[]',
    path TEXT,
    thumb TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    attribution TEXT DEFAULT '',
    monetization_allowed INTEGER DEFAULT 0,
    review TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER,
    platform TEXT,
    status TEXT,
    url TEXT DEFAULT '',
    external_id TEXT DEFAULT '',
    message TEXT DEFAULT '',
    created_at TEXT
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- низкоуровневое ----
    @staticmethod
    def _encode(fields: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in fields.items():
            if k in JSON_COLUMNS and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                v = int(v)
            out[k] = v
        return out

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_COLUMNS & d.keys():
            try:
                d[k] = json.loads(d[k]) if d[k] else ({} if k in {"params", "manual", "scores", "review"} else [])
            except (TypeError, json.JSONDecodeError):
                pass
        return d

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._decode(r) for r in rows]

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def insert(self, table: str, **fields: Any) -> int:
        fields = self._encode(fields)
        cols = ", ".join(f'"{c}"' for c in fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = self._encode(fields)
        sets = ", ".join(f'"{c}" = ?' for c in fields)
        with self._lock:
            self._conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))
            self._conn.commit()

    def get(self, table: str, row_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return self._decode(row)

    def delete(self, table: str, row_id: int) -> None:
        with self._lock:
            self._conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            self._conn.commit()

    # ---- задачи ----
    def job_log(self, job_id: int, message: str, **fields: Any) -> None:
        job = self.get("jobs", job_id)
        if not job:
            return
        log = list(job.get("log") or [])
        log.append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
        self.update("jobs", job_id, log=log[-200:], updated_at=now(), **fields)
