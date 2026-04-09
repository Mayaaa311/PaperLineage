from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CACHE_PATH = Path(os.getenv("LLM_CACHE_PATH", "llm_cache.db"))
_LOCK = threading.Lock()


def _ensure_db() -> None:
    with _LOCK:
        conn = sqlite3.connect(CACHE_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    cache_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def get_cached_json(cache_key: str) -> dict[str, Any] | None:
    _ensure_db()
    with _LOCK:
        conn = sqlite3.connect(CACHE_PATH)
        try:
            row = conn.execute(
                "SELECT value_json FROM llm_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    try:
        parsed = json.loads(row[0])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def set_cached_json(cache_key: str, value: dict[str, Any]) -> None:
    _ensure_db()
    payload = json.dumps(value)
    now = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        conn = sqlite3.connect(CACHE_PATH)
        try:
            conn.execute(
                """
                INSERT INTO llm_cache (cache_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key)
                DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (cache_key, payload, now),
            )
            conn.commit()
        finally:
            conn.close()
