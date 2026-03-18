from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_path() -> str:
    p = os.getenv("VIDEOMIND_DB_PATH", "data/videomind.sqlite3")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return p


def init_db() -> None:
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
              id TEXT PRIMARY KEY,
              original_url TEXT NOT NULL,
              title TEXT,
              description TEXT,
              tags_json TEXT,
              subtitles_json TEXT,
              category TEXT,
              key_points_json TEXT,
              summary TEXT,
              reminder_copy TEXT,
              remind_at TEXT,
              status TEXT NOT NULL,
              error_message TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(_db_path())
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def insert_pending(item: Dict[str, Any]) -> None:
    now = _utc_now_iso()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO collections (
              id, original_url, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (item["id"], item["original_url"], "pending", now, now),
        )
        conn.commit()


def update_item(id_: str, patch: Dict[str, Any]) -> None:
    if not patch:
        return
    patch = dict(patch)
    patch["updated_at"] = _utc_now_iso()

    cols = ", ".join([f"{k} = ?" for k in patch.keys()])
    vals = list(patch.values()) + [id_]
    with _conn() as conn:
        conn.execute(f"UPDATE collections SET {cols} WHERE id = ?", vals)
        conn.commit()


def get_item(id_: str) -> Optional[Dict[str, Any]]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (id_,)).fetchone()
        return dict(row) if row else None


def list_items_by_date_utc(date_yyyy_mm_dd: str) -> List[Dict[str, Any]]:
    # created_at 是 ISO UTC，比如 2026-03-19T12:34:56+00:00
    prefix = f"{date_yyyy_mm_dd}T"
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM collections
            WHERE created_at LIKE ?
            ORDER BY created_at DESC
            """,
            (prefix + "%",),
        ).fetchall()
        return [dict(r) for r in rows]

