"""Durable chat sessions, separate from the world database.

WhatsApp and deploys restart the process. Confirmation tokens stay in memory
(single worker), but history and conv_id must survive on disk so the next
message continues the same conversation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from nati import config


def _path() -> Path:
    return config.VAR_DIR / "sessions.db"


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
             phone TEXT PRIMARY KEY,
             conv_id TEXT NOT NULL,
             history_json TEXT NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS seen_messages (
             message_id TEXT PRIMARY KEY,
             seen_at TEXT NOT NULL
           )"""
    )
    return con


def load(phone: str) -> tuple[str | None, list[dict[str, Any]] | None]:
    """Returns (conv_id, history) or (None, None) if this phone is new."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT conv_id, history_json FROM sessions WHERE phone = ?",
            (phone,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None, None
    history = json.loads(row[1])
    if not isinstance(history, list):
        return row[0], []
    return row[0], history


def save(phone: str, conv_id: str, history: list[dict[str, Any]]) -> None:
    con = _connect()
    try:
        con.execute(
            """INSERT INTO sessions (phone, conv_id, history_json)
               VALUES (?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                 conv_id = excluded.conv_id,
                 history_json = excluded.history_json""",
            (phone, conv_id, json.dumps(history, ensure_ascii=False)),
        )
        con.commit()
    finally:
        con.close()


def claim_message(message_id: str) -> bool:
    """True if this Meta message id is new and now claimed. False if a retry."""
    if not message_id:
        return True
    con = _connect()
    try:
        con.execute(
            "INSERT INTO seen_messages (message_id, seen_at) VALUES (?, datetime('now'))",
            (message_id,),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()
