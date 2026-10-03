"""Очередь задач: один фоновый поток (GPU один), прогресс доступен через polling."""
from __future__ import annotations

import queue
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from . import config, presets, samples, sources
from .db import Library
from .engine import AceEngine, Cancelled

HISTORY_LIMIT = 30


@dataclass
class Job:
    id: str
    kind: str                       # generate | load
    request: dict[str, Any]
    status: str = "queued"          # queued | running | done | error | cancelled
    progress: float = 0.0
    stage: str = "В очереди"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    track_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        end = self.finished_at or now
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "error": self.error,
            "track_ids": self.track_ids,
            "elapsed": round(end - self.started_at, 1) if self.started_at else 0,
            "created_at": self.created_at,
            "title": _job_title(self),
            "sample": self.request.get("sample"),   # сэмпл библиотеки: {kind, id}; в архив не попадает
        }


def _job_title(job: Job) -> str:
    if job.kind == "load":
        return "Загрузка моделей"
    req = job.request
    return ((req.get("title") or "").strip() or _auto_title(req))[:60]


class JobManager:
    def __init__(self, engine: AceEngine, library: Library, get_settings) -> None:
        self.engine = engine
        self.library = library
        self.get_settings = get_settings
        self._q: queue.Queue[Job] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, name="job-worker", daemon=True)
        self._thread.start()

    # ---- API ----
    def submit(self, request: dict[str, Any], kind: str = "generate") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, request=request)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim()
        self._q.put(job)
        return job

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = (self._jobs[i] for i in reversed(self._order))
            # успешные задачи загрузки моделей — служебные, в очереди их не показываем
            return [j.to_dict() for j in jobs if not (j.kind == "load" and j.status == "done")]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def pending_samples(self) -> dict[str, str]:
        """{ключ сэмпла: id задачи} для сэмплов, которые ждут в очереди или генерируются."""
        with self._lock:
            return {samples.key(s["kind"], s["id"]): j.id for j in self._jobs.values()
                    if (s := j.request.get("sample")) and j.status in ("queued", "running")}

    def cancel(self, job_id: str) -> str | None:
        """Отменяет задачу в очереди, останавливает выполняющуюся или убирает завершённую из списка.

        Возвращает выполненное действие: "cancelled" | "stopping" | "removed" (None — задачи нет).
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status == "queued":
                job.status, job.stage, job.finished_at = "cancelled", "Отменено", time.time()
                return "cancelled"
            if job.status == "running":
                if job.kind != "generate":
                    return None   # загрузку моделей прерывать нельзя
                job.stage = "Останавливаю…"
                self.engine.request_cancel()
                return "stopping"
            self._jobs.pop(job_id, None)
            self._order.remove(job_id)
            return "removed"

    def forget_track(self, track_id: str) -> None:
        """Убирает трек из готовых задач очереди (дизлайк или удаление); опустевшая готовая задача исчезает."""
        with self._lock:
            for job_id in list(self._order):
                job = self._jobs[job_id]
                if track_id not in job.track_ids:
                    continue
                job.track_ids.remove(track_id)
                if not job.track_ids and job.status == "done":
                    self._jobs.pop(job_id, None)
                    self._order.remove(job_id)

    def _trim(self) -> None:
        finished = [i for i in self._order if self._jobs[i].status in ("done", "error", "cancelled")]
        for i in finished[: max(0, len(self._order) - HISTORY_LIMIT)]:
            self._jobs.pop(i, None)
            self._order.remove(i)

    # ---- worker ----
    def _worker(self) -> None:
        while True:
            job = self._q.get()
            if job.status == "cancelled":
                continue
            job.status, job.started_at, job.stage = "running", time.time(), "Запуск…"

            def progress(value: float, desc: str, _job: Job = job) -> None:
                _job.progress = max(_job.progress, min(1.0, value))
                _job.stage = desc

            try:
                if job.kind == "load":
                    self.engine.ensure_loaded(self.get_settings(), progress)
                else:
                    self._run_generate(job, progress)
                job.progress, job.status, job.stage = 1.0, "done", "Готово"
            except Cancelled:
                job.status, job.stage = "cancelled", "Отменено"
            except Exception as exc:  # noqa: BLE001
                logger.error("Задача {} упала: {}\n{}", job.id, exc, traceback.format_exc())
                job.status, job.error, job.stage = "error", str(exc) or type(exc).__name__, "Ошибка"
            finally:
                job.finished_at = time.time()

    def _run_generate(self, job: Job, progress) -> None:
        settings = self.get_settings()
        req = job.request
        tmp = config.TMP_DIR / job.id
        try:
            if req.get("task") == "cover":
                progress(0.01, "Подготовка исходника…")
                src, length = sources.cut_segment(self._source_path(req), req.get("start") or 0, req.get("end"), tmp)
                req = {**req, "_src_path": str(src), "duration": round(length, 2)}
            self._generate_and_store(job, req, settings, progress, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _source_path(self, req: dict[str, Any]) -> Path:
        if req.get("source_id"):
            try:
                return sources.source_file(req["source_id"])
            except KeyError as exc:
                raise sources.SourceError("Файл-исходник удалён") from exc
        track = self.library.get_track(req.get("source_track_id") or "")
        path = config.TRACKS_DIR / track["filename"] if track else None
        if not path or not path.exists():
            raise sources.SourceError("Трек-исходник удалён из архива")
        return path

    def _generate_and_store(self, job: Job, req: dict[str, Any], settings: dict[str, Any], progress,
                            tmp: Path) -> None:
        result = self.engine.generate(req, settings, progress, tmp)
        if req.get("sample"):
            progress(0.99, "Сохранение в библиотеку…")
            self._store_sample(req, result)
            return
        progress(0.99, "Сохранение в архив…")

        base_title = (req.get("title") or "").strip() or _auto_title(req)
        n = len(result["tracks"])
        for i, t in enumerate(result["tracks"], start=1):
            src = Path(t["path"])
            track_id = uuid.uuid4().hex[:16]
            dst = config.TRACKS_DIR / f"{track_id}{src.suffix}"
            shutil.move(str(src), str(dst))
            title = self.library.unique_title(base_title)   # занято → «… 1», «… 2»
            track = self.library.add_track(
                track_id=track_id,
                title=title,
                filename=dst.name,
                fmt=src.suffix.lstrip("."),
                folder_id=req.get("folder_id"),
                duration=t["duration"],
                seed=t.get("seed"),
                gen_seconds=result["gen_seconds"],
                prompt=req.get("prompt", ""),
                caption=result["caption"],
                lyrics=result["lyrics"],
                params={"request": {k: v for k, v in req.items() if not k.startswith("_")}, "negative": result["negative"],
                        "description_en": result["description_en"], "plan": result["plan"],
                        "stages": result.get("stages"), "time_costs": _round_costs(result.get("time_costs")),
                        "score": t.get("score"), "method": t.get("method"), "cover": _cover_info(req, result, t),
                        # фактические параметры генерации этого трека: всё, что ушло в ACE (seed, шаги, CFG, BPM и
                        # тональность от LM…), метаданные LM, конфиг батча и настройки движка на момент генерации
                        "generation": {"ace": t.get("ace") or {}, **(t.get("generation") or result.get("generation") or {})},
                        "batch_index": i, "batch_size": n, "ranked": result.get("ranked", False)},
            )
            job.track_ids.append(track["id"])

    @staticmethod
    def _store_sample(req: dict[str, Any], result: dict[str, Any]) -> None:
        t = result["tracks"][0]
        ace = t.get("ace") or {}
        lm = (result.get("generation") or {}).get("lm_metadata") or {}
        samples.store(req["sample"]["kind"], req["sample"]["id"], Path(t["path"]), {
            "duration": t["duration"],
            "seed": t.get("seed"),
            "gen_seconds": result["gen_seconds"],
            "caption": result["caption"],
            "negative": result["negative"],
            "lyrics": result["lyrics"],
            "dit": (result.get("plan") or {}).get("dit"),
            "bpm": ace.get("bpm") or ace.get("cot_bpm") or lm.get("bpm"),
            "keyscale": ace.get("keyscale") or ace.get("cot_keyscale") or lm.get("keyscale"),
        })


def _cover_info(req: dict[str, Any], result: dict[str, Any], track: dict[str, Any]) -> dict[str, Any] | None:
    """Параметры, специфичные для кавера: исходник, фрагмент, способ этого трека и что LM услышала в исходнике."""
    if req.get("task") != "cover":
        return None
    start = float(req.get("start") or 0)
    length = float(req.get("duration") or 0)
    return {
        **(result.get("cover") or {}),
        "method": track.get("method"),
        "source_id": req.get("source_id"),
        "source_track_id": req.get("source_track_id"),
        "source_name": req.get("source_name"),
        "start": round(start, 2),
        "end": round(start + length, 2),
        "length": round(length, 2),
        "strength": req.get("cover_strength"),
        "noise": req.get("cover_noise"),
    }


def _round_costs(costs: dict[str, Any] | None) -> dict[str, float]:
    return {k: round(v, 2) for k, v in (costs or {}).items() if isinstance(v, (int, float))}


def _auto_title(req: dict[str, Any]) -> str:
    if req.get("task") == "cover":
        return f"Кавер: {(req.get('source_name') or 'исходник')[:50]}"
    labels = {i: l for i, l, _ in presets.GENRES}
    genres = [labels[g] for g in req.get("genres", []) if g in labels]
    if genres:   # название по умолчанию — жанры
        return ", ".join(genres[:3])
    prompt = (req.get("prompt") or "").strip()
    if prompt:
        return prompt[:40] + ("…" if len(prompt) > 40 else "")
    return "Без названия"
