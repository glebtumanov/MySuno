"""Библиотека звучаний: минутные сэмплы жанров и настроений.

Сэмплы не попадают в архив: файлы лежат в data/samples, описание — в data/samples/index.json.
Генерируются по запросу пользователя (по одному), повторная генерация заменяет прежний сэмпл.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import config, presets

SAMPLES_DIR = config.DATA / "samples"
INDEX_FILE = SAMPLES_DIR / "index.json"
SAMPLE_SECONDS = 60
KINDS = ("genre", "mood")   # жанр | настроение

SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()


def _labels(kind: str) -> dict[str, str]:
    items = presets.MOODS if kind == "mood" else presets.GENRES
    return {i: label for i, label, _ in items}


def key(kind: str, preset_id: str) -> str:
    return f"{kind}-{preset_id}"


def validate(kind: str, preset_id: str) -> str:
    """Подпись пресета; KeyError — такого вида или пресета нет."""
    if kind not in KINDS:
        raise KeyError("Неизвестный вид сэмпла")
    label = _labels(kind).get(preset_id)
    if label is None:
        raise KeyError("Неизвестный жанр или настроение")
    return label


def title(kind: str, preset_id: str) -> str:
    return f"Сэмпл: {validate(kind, preset_id)}"


def language(kind: str, preset_id: str) -> str:
    """Язык вокала сэмпла: жанры — английский, кроме «национальных»; настроения — русский."""
    if kind == "mood":
        return "ru"
    return presets.GENRE_LANGUAGE.get(preset_id, "en")


def build_request(kind: str, preset_id: str) -> dict[str, Any]:
    """Запрос генерации сэмпла: только этот жанр / настроение, остальное решает LM (вокал, текст, темп)."""
    validate(kind, preset_id)
    return {
        "vocal_language": language(kind, preset_id),
        "prompt": "",
        "genres": [preset_id] if kind == "genre" else [],
        "moods": [preset_id] if kind == "mood" else [],
        # жанр без обязательного вокала — инструментальный сэмпл; настроения — с вокалом на выбор LM
        "vocal": "none" if kind == "genre" and preset_id not in presets.VOCAL_GENRES else "auto",
        "lyrics": "",
        "auto_lyrics": True,
        "thinking": True,
        "duration": SAMPLE_SECONDS,
        "temperature": 0.85,
        "guidance": 7.0,
        "batch": 1,
        "title": title(kind, preset_id),
        "sample": {"kind": kind, "id": preset_id},
    }


def _read_index() -> dict[str, Any]:
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(index: dict[str, Any]) -> None:
    tmp = INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INDEX_FILE)   # атомарно: обрыв посреди записи не портит индекс


def list_samples() -> dict[str, Any]:
    """{ключ: описание} только для сэмплов, файл которых на месте."""
    with _lock:
        index = _read_index()
    return {k: v for k, v in index.items() if (SAMPLES_DIR / v.get("filename", "")).is_file()}


def all_presets() -> list[tuple[str, str]]:
    """(вид, id) всех пресетов библиотеки в порядке показа."""
    return [(kind, i) for kind in KINDS for i in _labels(kind)]


def sample_file(kind: str, preset_id: str) -> Path:
    meta = list_samples().get(key(kind, preset_id))
    if not meta:
        raise KeyError("Сэмпла ещё нет")
    return SAMPLES_DIR / meta["filename"]


def store(kind: str, preset_id: str, src: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Перемещает готовый файл в библиотеку; прежний сэмпл того же пресета заменяется."""
    k = key(kind, preset_id)
    filename = f"{k}-{int(time.time())}{src.suffix}"   # новое имя: браузер не возьмёт старый файл из кэша
    shutil.move(str(src), str(SAMPLES_DIR / filename))
    entry = {**meta, "kind": kind, "id": preset_id, "filename": filename, "fmt": src.suffix.lstrip("."),
             "created_at": time.time()}
    with _lock:
        index = _read_index()
        old = index.get(k)
        index[k] = entry
        _write_index(index)
    if old and old.get("filename") != filename:
        (SAMPLES_DIR / old["filename"]).unlink(missing_ok=True)
    return entry


def delete(kind: str, preset_id: str) -> None:
    k = key(kind, preset_id)
    with _lock:
        index = _read_index()
        old = index.pop(k, None)
        if old is None:
            raise KeyError("Сэмпла нет")
        _write_index(index)
    (SAMPLES_DIR / old["filename"]).unlink(missing_ok=True)


def delete_all() -> int:
    """Удаляет все сэмплы; возвращает их число."""
    with _lock:
        index = _read_index()
        _write_index({})
    for meta in index.values():
        if meta.get("filename"):
            (SAMPLES_DIR / meta["filename"]).unlink(missing_ok=True)
    return len(index)
