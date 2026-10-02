"""SQLite-хранилище: одноуровневые папки и треки."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    folder_id   INTEGER REFERENCES folders(id) ON DELETE SET NULL,
    filename    TEXT NOT NULL,
    fmt         TEXT NOT NULL,
    duration    REAL,
    seed        INTEGER,
    gen_seconds REAL,
    prompt      TEXT,
    caption     TEXT,
    lyrics      TEXT,
    params      TEXT,
    created_at  REAL NOT NULL,
    rating      INTEGER NOT NULL DEFAULT 0   -- 1 лайк, -1 дизлайк, 0 без оценки
);
CREATE INDEX IF NOT EXISTS idx_tracks_folder ON tracks(folder_id);
CREATE INDEX IF NOT EXISTS idx_tracks_created ON tracks(created_at DESC);
"""

# Фильтры по оценке: условие на столбец rating (алиас таблицы подставляется)
_RATING_FILTERS = {
    "all": "",
    "visible": "{t}rating >= 0",      # по умолчанию: без дизлайков
    "liked": "{t}rating = 1",
    "disliked": "{t}rating = -1",
    "unrated": "{t}rating = 0",
}


def _rating_cond(rating: str, alias: str = "") -> str:
    if rating not in _RATING_FILTERS:
        raise ValueError("Неизвестный фильтр оценки")
    return _RATING_FILTERS[rating].format(t=alias)


class Library:
    """Потокобезопасная обёртка над sqlite3."""

    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tracks)")}
        if "rating" not in cols:   # миграция БД, созданной до появления оценок
            self._conn.execute("ALTER TABLE tracks ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
        self._conn.commit()

    # ---- папки ---------------------------------------------------------
    def list_folders(self, rating: str = "all") -> dict[str, Any]:
        """Папки со счётчиками + общее число треков и треков без папки (с учётом фильтра оценки)."""
        cond = _rating_cond(rating)
        join_cond = f" AND {_rating_cond(rating, 't.')}" if cond else ""
        and_cond = f" AND {cond}" if cond else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.id, f.name, COUNT(t.id) AS count FROM folders f "
                f"LEFT JOIN tracks t ON t.folder_id = f.id{join_cond} GROUP BY f.id ORDER BY f.name COLLATE NOCASE"
            ).fetchall()
            total = self._conn.execute(f"SELECT COUNT(*) FROM tracks WHERE 1=1{and_cond}").fetchone()[0]
            unfiled = self._conn.execute(
                f"SELECT COUNT(*) FROM tracks WHERE folder_id IS NULL{and_cond}"
            ).fetchone()[0]
        return {"folders": [dict(r) for r in rows], "total": total, "unfiled": unfiled}

    def create_folder(self, name: str) -> dict[str, Any]:
        name = _clean_name(name)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO folders(name, created_at) VALUES (?, ?)", (name, time.time())
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Папка с таким именем уже существует") from exc
            self._conn.commit()
            return {"id": cur.lastrowid, "name": name, "count": 0}

    def rename_folder(self, folder_id: int, name: str) -> None:
        name = _clean_name(name)
        with self._lock:
            try:
                cur = self._conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("Папка с таким именем уже существует") from exc
            if cur.rowcount == 0:
                raise KeyError("Папка не найдена")
            self._conn.commit()

    def delete_folder(self, folder_id: int) -> None:
        """Удаляет папку; её треки переходят в «Без папки» (ON DELETE SET NULL)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            if cur.rowcount == 0:
                raise KeyError("Папка не найдена")
            self._conn.commit()

    # ---- треки ---------------------------------------------------------
    def add_track(
        self,
        *,
        title: str,
        filename: str,
        fmt: str,
        folder_id: int | None = None,
        duration: float | None = None,
        seed: int | None = None,
        gen_seconds: float | None = None,
        prompt: str = "",
        caption: str = "",
        lyrics: str = "",
        params: dict[str, Any] | None = None,
        track_id: str | None = None,
    ) -> dict[str, Any]:
        track_id = track_id or uuid.uuid4().hex[:16]
        with self._lock:
            self._conn.execute(
                "INSERT INTO tracks(id, title, folder_id, filename, fmt, duration, seed, gen_seconds,"
                " prompt, caption, lyrics, params, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    track_id, title, folder_id, filename, fmt, duration, seed, gen_seconds,
                    prompt, caption, lyrics, json.dumps(params or {}, ensure_ascii=False), time.time(),
                ),
            )
            self._conn.commit()
        return self.get_track(track_id)  # type: ignore[return-value]

    def get_track(self, track_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return _track(row) if row else None

    def list_tracks(self, folder: str | int | None = "all", search: str = "",
                    rating: str = "all") -> list[dict[str, Any]]:
        """folder: "all" — все, "none" — без папки, число — id папки; rating — ключ _RATING_FILTERS."""
        sql, args = "SELECT * FROM tracks WHERE 1=1", []
        if cond := _rating_cond(rating):
            sql += f" AND {cond}"
        if folder == "none":
            sql += " AND folder_id IS NULL"
        elif folder not in (None, "all"):
            sql += " AND folder_id = ?"
            args.append(int(folder))
        if search.strip():
            sql += " AND (title LIKE ? OR prompt LIKE ? OR caption LIKE ?)"
            like = f"%{search.strip()}%"
            args += [like, like, like]
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_track(r) for r in rows]

    def update_track(self, track_id: str, *, title: str | None = None,
                     folder_id: int | None | str = "__keep__", rating: int | None = None) -> dict[str, Any]:
        sets, args = [], []
        if title is not None:
            sets.append("title = ?")
            args.append(_clean_name(title, "Название"))
        if rating is not None:
            if rating not in (-1, 0, 1):
                raise ValueError("Оценка должна быть -1, 0 или 1")
            sets.append("rating = ?")
            args.append(rating)
        if folder_id != "__keep__":
            if folder_id is not None:
                with self._lock:
                    if not self._conn.execute("SELECT 1 FROM folders WHERE id = ?", (folder_id,)).fetchone():
                        raise KeyError("Папка не найдена")
            sets.append("folder_id = ?")
            args.append(folder_id)
        if sets:
            with self._lock:
                cur = self._conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id = ?", (*args, track_id))
                if cur.rowcount == 0:
                    raise KeyError("Трек не найден")
                self._conn.commit()
        track = self.get_track(track_id)
        if track is None:
            raise KeyError("Трек не найден")
        return track

    def delete_track(self, track_id: str) -> str | None:
        """Удаляет запись и возвращает имя файла (файл удаляет вызывающий)."""
        with self._lock:
            row = self._conn.execute("SELECT filename FROM tracks WHERE id = ?", (track_id,)).fetchone()
            if not row:
                raise KeyError("Трек не найден")
            self._conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
            self._conn.commit()
        return row["filename"]


def _clean_name(name: str, what: str = "Имя папки") -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError(f"{what} не может быть пустым")
    return name[:80]


def _track(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["params"] = json.loads(d.get("params") or "{}")
    except json.JSONDecodeError:
        d["params"] = {}
    return d


_instance: Library | None = None


def get_library() -> Library:
    global _instance
    if _instance is None:
        _instance = Library(config.DB_FILE)
    return _instance
