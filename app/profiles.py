"""SQLite-backed server profiles. Private keys and passphrases are never stored here."""
from __future__ import annotations

import os
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager

from .config import state_path
from .models import ServerProfile


@contextmanager
def database():
    path = state_path("PROFILE_DB_PATH", "state/profiles.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create the file with restricted permissions before SQLite opens it (POSIX).
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path, timeout=15)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        with connection:
            yield connection
    finally:
        connection.close()


def list_profiles() -> list[dict]:
    with database() as connection:
        rows = connection.execute("SELECT id, payload FROM profiles ORDER BY rowid").fetchall()
    items = []
    for profile_id, payload in rows:
        profile = ServerProfile.model_validate_json(payload)
        items.append({"id": profile_id, "revision": revision(profile), **profile.model_dump()})
    return items


def revision(profile: ServerProfile) -> str:
    return hashlib.sha256(profile.model_dump_json().encode()).hexdigest()


def get_profile(profile_id: str) -> ServerProfile:
    with database() as connection:
        row = connection.execute("SELECT payload FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if row is None:
        raise KeyError(profile_id)
    return ServerProfile.model_validate_json(row[0])


def save_profile(profile: ServerProfile, profile_id: str | None = None) -> str:
    with database() as connection:
        if profile_id is None:
            profile_id = uuid.uuid4().hex
            connection.execute("INSERT INTO profiles VALUES (?, ?)", (profile_id, profile.model_dump_json()))
        else:
            result = connection.execute("UPDATE profiles SET payload = ? WHERE id = ?", (profile.model_dump_json(), profile_id))
            if result.rowcount == 0:
                raise KeyError(profile_id)
    return profile_id


def delete_profile(profile_id: str) -> None:
    with database() as connection:
        result = connection.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        if result.rowcount == 0:
            raise KeyError(profile_id)
