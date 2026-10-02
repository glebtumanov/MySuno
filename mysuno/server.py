"""FastAPI-приложение MySuno (только localhost, один пользователь)."""
from __future__ import annotations

import mimetypes
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, presets, translate
from . import lyrics as lyrics_tools
from .db import get_library
from .engine import get_engine, hardware
from .jobs import JobManager

mimetypes.add_type("audio/flac", ".flac")

library = get_library()
engine = get_engine()
jobs: JobManager | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global jobs
    jobs = JobManager(engine, library, config.load_settings)
    settings = config.load_settings()

    def background_start() -> None:
        # Последовательно: ACE на время загрузки DiT меняет глобальный default dtype, поэтому переводчик
        # (CPU, ~2 с) прогреваем ДО загрузки моделей, а не параллельно с ней.
        if settings.get("translate", True):
            translate.warmup()
        if settings.get("preload", True):
            jobs.submit({}, kind="load")   # модели грузятся и прогреваются в фоне — первый трек стартует сразу

    threading.Thread(target=background_start, name="background-start", daemon=True).start()
    yield


app = FastAPI(title="MySuno", version=__version__, lifespan=lifespan)


# ------------------------------------------------------------------ модели запросов
class GenerateRequest(BaseModel):
    prompt: str = Field("", max_length=2000)
    genres: list[str] = []
    negative_genres: list[str] = []
    moods: list[str] = []
    vocal: str = "auto"
    instrumental: bool = False
    lyrics: str = Field("", max_length=4096)
    auto_lyrics: bool = True
    thinking: bool = True
    duration: float = Field(60, ge=10, le=600)
    temperature: float = Field(0.85, ge=0, le=2)
    lm_cfg_scale: float | None = Field(None, ge=1, le=10)   # CFG LM-планировщика (None = 4.0 по умолчанию движка)
    steps: int | None = Field(None, ge=1, le=200)
    guidance: float = Field(7.0, ge=1, le=15)
    seed: int | None = None
    batch: int = Field(1, ge=1, le=8)
    rank: bool = False              # выбрать лучший из `batch` вариантов по оценке качества
    keep_all: bool = False          # при rank сохранить и остальные варианты
    cot_caption: bool = True        # LM переписывает caption в «родной» для DiT вид
    guard: bool = True              # перегенерировать одиночный трек при шуме/дроне
    lm_rep_penalty: float | None = Field(None, ge=1.0, le=1.5)   # None = по умолчанию движка
    lm_top_p: float | None = Field(None, gt=0, le=1)
    adg: bool | None = None
    shift: float | None = Field(None, ge=1, le=5)
    bpm: int | None = Field(None, ge=30, le=300)
    format: str | None = None
    title: str = Field("", max_length=120)
    folder_id: int | None = None


class FolderBody(BaseModel):
    name: str


class TrackPatch(BaseModel):
    title: str | None = None
    folder_id: int | None = None
    move: bool = False        # True — применить folder_id (в т.ч. null = «Без папки»)
    rating: int | None = Field(None, ge=-1, le=1)   # 1 лайк, -1 дизлайк (убирает из очереди), 0 сброс


# ------------------------------------------------------------------ система
@app.get("/api/info")
def info():
    return {"version": __version__, "hardware": hardware(), "engine": engine.status()}


@app.get("/api/presets")
def get_presets():
    return presets.presets_payload()


@app.get("/api/settings")
def get_settings():
    return config.load_settings()


@app.put("/api/settings")
def put_settings(patch: dict):
    try:
        return config.update_settings(patch)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/engine/load")
def engine_load():
    return jobs.submit({}, kind="load").to_dict()


@app.post("/api/engine/unload")
def engine_unload():
    if engine.busy():
        raise HTTPException(409, "Движок занят генерацией")
    engine.unload()
    return engine.status()


# ------------------------------------------------------------------ тексты и проверки
class TextBody(BaseModel):
    text: str = Field("", max_length=8000)


@app.post("/api/lyrics/normalize")
def normalize_lyrics(body: TextBody):
    """Приводит текст к виду, который лучше понимает модель (английские теги секций), и проверяет метрику строк."""
    text, warnings = lyrics_tools.normalize(body.text)
    return {"text": text, "warnings": warnings}


class TagsBody(BaseModel):
    genres: list[str] = []
    moods: list[str] = []


@app.post("/api/check")
def check_tags(body: TagsBody):
    return {"warnings": presets.check_conflicts(body.genres, body.moods)}


# ------------------------------------------------------------------ генерация
@app.post("/api/generate")
def generate(body: GenerateRequest):
    settings = config.load_settings()
    if body.duration > settings["max_duration"]:
        raise HTTPException(400, f"Длительность больше лимита из настроек ({settings['max_duration']} с)")
    if not (body.prompt.strip() or body.genres or body.lyrics.strip()):
        raise HTTPException(400, "Опишите музыку, выберите жанр или введите текст песни")
    request = body.model_dump()
    warnings = presets.check_conflicts(body.genres, body.moods)
    if body.lyrics.strip():
        request["lyrics"], lyric_warnings = lyrics_tools.normalize(body.lyrics)
        warnings += lyric_warnings
    if body.duration > 240:
        warnings.append("Треки длиннее 4 минут чаще получаются с повторами и «кашей» — надёжнее 1–3 минуты")
    job = jobs.submit(request).to_dict()
    job["warnings"] = warnings
    return job


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs.list(), "engine": engine.status()}


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    action = jobs.cancel(job_id)
    if action is None:
        raise HTTPException(409, "Эту задачу нельзя отменить")
    return {"ok": True, "action": action}


# ------------------------------------------------------------------ папки
@app.get("/api/folders")
def list_folders(rating: str = "all"):
    try:
        return library.list_folders(rating)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/folders")
def create_folder(body: FolderBody):
    try:
        return library.create_folder(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/folders/{folder_id}")
def rename_folder(folder_id: int, body: FolderBody):
    try:
        library.rename_folder(folder_id, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int):
    try:
        library.delete_folder(folder_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


# ------------------------------------------------------------------ треки
@app.get("/api/tracks")
def list_tracks(folder: str = "all", q: str = "", rating: str = "all"):
    try:
        return {"tracks": library.list_tracks(folder, q, rating)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/tracks/{track_id}")
def patch_track(track_id: str, body: TrackPatch):
    try:
        if body.move:
            track = library.update_track(track_id, title=body.title, folder_id=body.folder_id, rating=body.rating)
        else:
            track = library.update_track(track_id, title=body.title, rating=body.rating)
        if body.rating == -1:
            jobs.forget_track(track_id)
        return track
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/tracks/{track_id}")
def delete_track(track_id: str):
    try:
        filename = library.delete_track(track_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    jobs.forget_track(track_id)
    if filename:
        (config.TRACKS_DIR / filename).unlink(missing_ok=True)
    return {"ok": True}


def _track_file(track_id: str):
    track = library.get_track(track_id)
    if not track:
        raise HTTPException(404, "Трек не найден")
    path = config.TRACKS_DIR / track["filename"]
    if not path.exists():
        raise HTTPException(404, "Файл трека отсутствует")
    return track, path


@app.get("/api/tracks/{track_id}/audio")
def track_audio(track_id: str):
    track, path = _track_file(track_id)
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media)


@app.get("/api/tracks/{track_id}/download")
def track_download(track_id: str):
    track, path = _track_file(track_id)
    return FileResponse(path, filename=f"{track['title']}.{track['fmt']}")


# ------------------------------------------------------------------ статика
@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


class _NoCacheStatic(StaticFiles):
    """Статика всегда ревалидируется (ETag), чтобы правки UI применялись сразу."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _NoCacheStatic(directory=config.WEB_DIR), name="static")
