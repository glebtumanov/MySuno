"""Исходники для каверов: загруженные пользователем файлы и вырезка фрагмента.

Файл хранится как data/sources/<id><ext>, рядом <id>.json с метаданными. Читаем через soundfile (libsndfile:
WAV/FLAC/OGG/OPUS/MP3); форматы, которые он не понимает (M4A/AAC/WebM), браузер декодирует сам и присылает WAV.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

MAX_UPLOAD_BYTES = 200 * 2**20
MIN_SECONDS = 5.0
_ID = re.compile(r"^[0-9a-f]{16}$")
_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".oga", ".opus", ".aiff", ".aif"}


class SourceError(ValueError):
    """Понятная пользователю ошибка исходника."""


class UnsupportedAudio(SourceError):
    """soundfile не читает этот формат — клиенту стоит прислать WAV."""


def _meta_path(source_id: str) -> Path:
    return config.SOURCES_DIR / f"{source_id}.json"


def _check_id(source_id: str) -> str:
    if not _ID.match(source_id or ""):
        raise KeyError("Исходник не найден")
    return source_id


def save_upload(data: bytes, filename: str) -> dict[str, Any]:
    """Сохраняет загруженный файл и возвращает метаданные {id, name, duration, ...}."""
    import soundfile as sf

    if not data:
        raise SourceError("Пустой файл")
    if len(data) > MAX_UPLOAD_BYTES:
        raise SourceError(f"Файл больше {MAX_UPLOAD_BYTES // 2**20} МБ")
    ext = Path(filename or "").suffix.lower()
    if ext not in _EXTS:
        raise UnsupportedAudio(f"Формат {ext or '?'} читается только через браузер")
    source_id = uuid.uuid4().hex[:16]
    path = config.SOURCES_DIR / f"{source_id}{ext}"
    path.write_bytes(data)
    try:
        info = sf.info(str(path))
        duration = float(info.frames) / info.samplerate if info.frames > 0 else float(info.duration)
    except Exception as exc:  # noqa: BLE001 — любая ошибка libsndfile = формат не поддержан
        path.unlink(missing_ok=True)
        raise UnsupportedAudio(f"Не удалось прочитать аудио: {exc}") from exc
    if duration < MIN_SECONDS:
        path.unlink(missing_ok=True)
        raise SourceError(f"Слишком короткий файл ({duration:.1f} с) — нужно хотя бы {MIN_SECONDS:.0f} с")
    name = Path(filename).stem.strip()[:120] or "Исходник"
    meta = {"id": source_id, "name": name, "file": path.name, "duration": round(duration, 2),
            "sample_rate": info.samplerate, "channels": info.channels, "created_at": time.time()}
    _meta_path(source_id).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def get_source(source_id: str) -> dict[str, Any]:
    meta_file = _meta_path(_check_id(source_id))
    if not meta_file.exists():
        raise KeyError("Исходник не найден")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    path = config.SOURCES_DIR / meta["file"]
    if not path.exists():
        raise KeyError("Файл исходника удалён")
    meta["fmt"] = path.suffix.lstrip(".").upper()
    meta["size"] = path.stat().st_size
    return meta


def source_file(source_id: str) -> Path:
    return config.SOURCES_DIR / get_source(source_id)["file"]


def list_sources() -> list[dict[str, Any]]:
    out = []
    for meta_file in config.SOURCES_DIR.glob("*.json"):
        try:
            out.append(get_source(meta_file.stem))
        except (KeyError, ValueError, OSError):
            continue
    return sorted(out, key=lambda m: -m["created_at"])


def rename_source(source_id: str, name: str) -> dict[str, Any]:
    meta = get_source(source_id)
    name = " ".join((name or "").split())[:120]
    if not name:
        raise SourceError("Название не может быть пустым")
    stored = {k: v for k, v in meta.items() if k not in ("fmt", "size")}   # вычисляемые поля не храним
    stored["name"] = name
    _meta_path(source_id).write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    return {**meta, "name": name}


def delete_source(source_id: str) -> None:
    meta = get_source(source_id)
    (config.SOURCES_DIR / meta["file"]).unlink(missing_ok=True)
    _meta_path(source_id).unlink(missing_ok=True)


def cut_segment(src: Path, start: float, end: float | None, out_dir: Path) -> tuple[Path, float]:
    """Вырезает [start, end) в WAV (или отдаёт исходник целиком, если вырезать нечего). Возвращает (путь, длительность)."""
    import numpy as np
    import soundfile as sf

    info = sf.info(str(src))
    sr = info.samplerate
    total = info.frames / sr if info.frames > 0 else float(info.duration)
    start = max(0.0, float(start or 0))
    end = total if end is None or end <= 0 else min(float(end), total)
    if end - start < MIN_SECONDS:
        raise SourceError(f"Фрагмент короче {MIN_SECONDS:.0f} с")
    if start < 0.05 and end > total - 0.05:
        return src, total
    data, _ = sf.read(str(src), start=int(start * sr), stop=int(end * sr), dtype="float32", always_2d=True)
    fade = min(len(data) // 4, int(0.02 * sr))   # 20 мс, чтобы срез не щёлкал
    if fade > 0:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
        if start > 0.05:
            data[:fade] *= ramp
        if end < total - 0.05:
            data[-fade:] *= ramp[::-1]
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "source.wav"
    sf.write(str(dst), data, sr, subtype="PCM_16")
    return dst, len(data) / sr
