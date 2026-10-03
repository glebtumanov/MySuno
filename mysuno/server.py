"""FastAPI-приложение MySuno (только localhost, один пользователь)."""
from __future__ import annotations

import json
import mimetypes
import threading
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, config, presets, sources, translate
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
    keyscale: str = Field("", max_length=40)   # тональность («C major»); пусто — решает LM
    format: str | None = None
    title: str = Field("", max_length=120)
    folder_id: int | None = None


class CoverRequest(GenerateRequest):
    """Кавер: исходник задаёт мелодию, ритм и структуру; стиль — жанры/описание; текст пуст → инструментал."""
    source_id: str | None = None          # загруженный файл (data/sources)
    source_track_id: str | None = None    # или трек из архива
    source_name: str = Field("", max_length=120)
    start: float = Field(0, ge=0)          # фрагмент исходника, с
    end: float | None = Field(None, ge=0)  # None = до конца
    cover_mode: str = Field("auto", pattern="^(auto|cover|edit)$")   # авто | по нотам (cover) | перекраска (FlowEdit)
    cover_strength: float = Field(0.6, ge=0, le=1)   # близость: доля шагов cover / окно FlowEdit
    cover_noise: float = Field(0.0, ge=0, le=1)      # старт с зашумлённого исходника: ближе звучание/тембр


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


UI_STATE_LIMIT = 256 * 1024


@app.get("/api/ui-state")
def get_ui_state():
    """Последнее содержимое форм (без него страница берёт значения по умолчанию)."""
    try:
        return json.loads(config.UI_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@app.put("/api/ui-state")
async def put_ui_state(request: Request):
    """Сохраняет состояние форм: ключи верхнего уровня (create, cover) заменяются целиком."""
    raw = await request.body()
    if len(raw) > UI_STATE_LIMIT:
        raise HTTPException(413, "Слишком большое состояние формы")
    try:
        patch = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "Некорректный JSON") from exc
    if not isinstance(patch, dict):
        raise HTTPException(400, "Ожидается объект")
    state = {**get_ui_state(), **{k: v for k, v in patch.items() if k in ("create", "cover")}}
    tmp = config.UI_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.UI_STATE_FILE)   # атомарно: обрыв посреди записи не портит файл
    return {"ok": True}


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


# ------------------------------------------------------------------ каверы
@app.post("/api/sources")
async def upload_source(request: Request):
    """Тело запроса — сам файл; имя в заголовке X-Filename (URL-encoded). 415 — пусть браузер пришлёт WAV."""
    data = await request.body()
    try:
        return sources.save_upload(data, unquote(request.headers.get("x-filename", "")))
    except sources.UnsupportedAudio as exc:
        raise HTTPException(415, str(exc)) from exc
    except sources.SourceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/sources")
def list_sources():
    """Загруженные исходники + сколько каверов из каждого сделано (по параметрам треков архива)."""
    used: dict[str, int] = {}
    for track in library.list_tracks("all"):
        req = (track.get("params") or {}).get("request") or {}
        if req.get("task") == "cover" and req.get("source_id"):
            used[req["source_id"]] = used.get(req["source_id"], 0) + 1
    return {"sources": [{**s, "covers": used.get(s["id"], 0)} for s in sources.list_sources()]}


class SourcePatch(BaseModel):
    name: str = Field(..., max_length=120)


@app.patch("/api/sources/{source_id}")
def rename_source(source_id: str, body: SourcePatch):
    try:
        return sources.rename_source(source_id, body.name)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except sources.SourceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/sources/{source_id}/audio")
def source_audio(source_id: str):
    try:
        path = sources.source_file(source_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: str):
    try:
        sources.delete_source(source_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@app.post("/api/cover")
def cover(body: CoverRequest):
    settings = config.load_settings()
    if bool(body.source_id) == bool(body.source_track_id):
        raise HTTPException(400, "Загрузите файл или выберите трек из архива")
    try:
        if body.source_id:
            total = sources.get_source(body.source_id)["duration"]
        else:
            track = library.get_track(body.source_track_id)
            if not track or not (config.TRACKS_DIR / track["filename"]).exists():
                raise KeyError("Трек-исходник не найден")
            total = track["duration"] or 0
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    end = min(body.end, total) if body.end else total
    length = end - body.start
    if length < sources.MIN_SECONDS:
        raise HTTPException(400, f"Фрагмент слишком короткий — нужно хотя бы {sources.MIN_SECONDS:.0f} с")
    if length > settings["max_duration"] + 1:
        raise HTTPException(400, f"Фрагмент длиннее лимита из настроек ({settings['max_duration']} с) — выделите часть")
    request = body.model_dump()
    request.update(task="cover", duration=round(length, 2), auto_lyrics=False, thinking=False)
    warnings = presets.check_conflicts(body.genres, body.moods)
    if body.lyrics.strip():
        request["lyrics"], lyric_warnings = lyrics_tools.normalize(body.lyrics)
        warnings += lyric_warnings
    if length > 240:
        warnings.append("Фрагменты длиннее 4 минут чаще получаются с «кашей» — надёжнее 1–3 минуты")
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
