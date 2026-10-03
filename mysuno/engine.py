"""Движок ACE-Step 1.5: определение железа, загрузка моделей, генерация."""
from __future__ import annotations

import contextlib
import gc
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from . import config, presets, translate
from .audio import convert_to_mp3

ProgressCb = Callable[[float, str], None]

TURBO_STEPS = 8
QUALITY_STEPS = 50     # sft/base по документации ACE: 32–64 шага
EDIT_STEPS = 60        # FlowEdit на sft/base: документация ACE рекомендует ≥60
MAX_NOISE_RETRIES = 2
LYRICS_RETRIES = 2       # сколько раз переспросить LM, если вместо текста песни она вернула одни теги
LYRICS_LANGUAGES = {"en": "English", "ru": "Russian", "fr": "French", "es": "Spanish", "pt": "Portuguese", "it": "Italian",
                    "de": "German", "ko": "Korean", "ja": "Japanese", "la": "Latin", "uk": "Ukrainian", "zh": "Chinese"}
NOISE_RATIO_LIMIT = 0.25   # доля 10-с окон с флэтностью > 0.15, после которой результат считаем «кашей»
NOISE_FLAT_LIMIT = 0.10    # или медианная флэтность по треку
STATIC_LIMIT = 0.60        # доля почти неизменных соседних 10-с окон = «дрон» (у нормальных треков < 0.3)
LM_BATCH_CHUNK = 4         # элементов за проход LM при батче (с CFG = 8 строк → быстрый декодер)
MAX_BATCH = 16           # кандидатов на один трек: ACE даёт ≤8 за проход, остальное догенерируем порциями
MAX_EXTRA_BATCH_CALLS = 15  # сколько раз догенерировать недостающих кандидатов (×16 порциями по 1 — 1+15)
LM_CFG_DEFAULT = 4.0   # ACE по умолчанию 2.0; на замерах (300 с) 2.0 → 7.18, ≥3 → 7.6–7.7: CFG LM подавляет зацикливание кодов


class EngineError(RuntimeError):
    """Ожидаемая ошибка движка с понятным пользователю сообщением."""


class Cancelled(Exception):
    """Пользователь отменил генерацию."""


_FATAL_CUDA = ("device-side assert", "illegal memory access", "cuda error", "cublas_status", "unspecified launch failure")


# --------------------------------------------------------------------------- железо
def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except OSError:
        import platform

        return platform.processor() or "CPU"


_hw_cache: dict[str, Any] | None = None


def hardware(refresh: bool = False) -> dict[str, Any]:
    """Описание железа: CPU, RAM, CUDA-устройства."""
    global _hw_cache
    if _hw_cache is not None and not refresh:
        info = dict(_hw_cache)
    else:
        import psutil
        import torch

        info = {
            "cpu_name": _cpu_name(),
            "cores": psutil.cpu_count(logical=False) or os.cpu_count(),
            "threads": psutil.cpu_count(logical=True) or os.cpu_count(),
            "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
            "torch": torch.__version__,
            "cuda": bool(torch.cuda.is_available()),
            "cuda_version": torch.version.cuda,
            "gpus": [],
        }
        if info["cuda"]:
            props = torch.cuda.get_device_properties(0)
            info["gpus"].append({
                "name": props.name,
                "vram_gb": round(props.total_memory / 2**30, 1),
                "capability": f"{props.major}.{props.minor}",
            })
        _hw_cache = dict(info)
    if info["cuda"]:
        import torch

        try:
            free, total = torch.cuda.mem_get_info(0)
            info["gpus"][0]["free_gb"] = round(free / 2**30, 1)
            info["gpus"][0]["vram_gb"] = round(total / 2**30, 1)
        except RuntimeError:   # контекст CUDA сломан (фатальная ошибка) — статус всё равно должен отдаваться
            info["gpus"][0]["free_gb"] = None
    return info


# --------------------------------------------------------------------------- план
def resolve_plan(settings: dict[str, Any], safe: bool = False, loaded: bool = False) -> dict[str, Any]:
    """Превращает настройки («авто» и т.п.) в конкретные параметры загрузки моделей.

    loaded=True — наши модели уже в VRAM, поэтому свободную память считаем равной полной
    (иначе план «поплыл» бы после загрузки и модели перезагружались бы на каждой задаче).
    """
    hw = hardware()
    device = settings["device"]
    if device == "auto":
        device = "cuda" if hw["cuda"] else "cpu"
    elif device == "cuda" and not hw["cuda"]:
        logger.warning("CUDA недоступна — переключаюсь на CPU")
        device = "cpu"

    from acestep.gpu_config import get_gpu_config, resolve_lm_backend, set_global_gpu_config

    lm_setting = settings["lm_model"]
    if device == "cpu":
        gcfg = get_gpu_config(0)
        set_global_gpu_config(gcfg)
        lm = None if lm_setting in ("auto", "none") else lm_setting
        return {"device": "cpu", "dit": settings["dit_model"], "lm": lm, "backend": "pt",
                "offload": False, "quant": None, "fast_lm": False}

    gpu = hw["gpus"][0]
    gcfg = get_gpu_config(gpu["vram_gb"])
    set_global_gpu_config(gcfg)

    if lm_setting == "auto":
        lm = gcfg.recommended_lm_model if gcfg.init_lm_default else None
    elif lm_setting == "none":
        lm = None
    else:
        lm = lm_setting

    backend = settings["lm_backend"]
    backend = resolve_lm_backend(None if backend == "auto" else backend, gcfg)

    # 16-ГБ карта с большим запасом свободной памяти держит всё на GPU без offload и квантования.
    free_gb = gpu["vram_gb"] if loaded else gpu.get("free_gb", 0)
    roomy = gpu["vram_gb"] >= 15 and free_gb >= 11
    offload = (not roomy) and gcfg.offload_to_cpu_default if settings["offload"] == "auto" \
        else settings["offload"] == "on"
    if settings["quantization"] == "auto":
        quant = "int8_weight_only" if (not roomy and gcfg.quantization_default) else None
    else:
        quant = "int8_weight_only" if settings["quantization"] == "int8" else None

    if safe:  # повтор после нехватки VRAM
        offload, quant = True, "int8_weight_only"
        if lm:
            lm = "acestep-5Hz-lm-0.6B"
    return {"device": "cuda", "dit": settings["dit_model"], "lm": lm, "backend": backend,
            "offload": offload, "quant": quant,
            "fast_lm": bool(settings.get("fast_lm", True)) and not offload}


# --------------------------------------------------------------------------- движок
class AceEngine:
    """Держит DiT и LM резидентно между задачами."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.dit = None
        self.llm = None
        self.plan: dict[str, Any] | None = None       # фактически загруженная конфигурация
        self._requested: dict[str, Any] | None = None  # план до загрузки (для сравнения с настройками)
        self._safe_mode = False                        # включается после нехватки VRAM
        self._cancel = threading.Event()               # запрос отмены текущей генерации
        self._fatal: str | None = None                 # фатальная ошибка CUDA: помогает только перезапуск процесса
        self._understood: dict[tuple, dict[str, Any]] = {}   # описание исходников каверов от LM (по фрагменту)
        self.state = "unloaded"      # unloaded | loading | ready | error
        self.last_error: str | None = None
        self.load_seconds: float | None = None
        self.warmup_seconds: float | None = None

    # ---- отмена ----
    def request_cancel(self) -> None:
        """Просит текущую генерацию остановиться: проверяется на каждом токене LM и в колбэках прогресса."""
        self._cancel.set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    # ---- загрузка ----
    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "plan": self.plan,
            "error": self.last_error,
            "fatal": self._fatal is not None,
            "load_seconds": self.load_seconds,
            "warmup_seconds": self.warmup_seconds,
            "lm_ready": bool(self.llm is not None and getattr(self.llm, "llm_initialized", False)),
        }

    def busy(self) -> bool:
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return False
        return True

    def unload(self, reset_safe: bool = True) -> None:
        with self._lock:
            if reset_safe:
                self._safe_mode = False
            self._release()
            self.state = "unloaded"
            self.plan = None
            self._requested = None

    def _release(self) -> None:
        if self.llm is not None:
            try:
                self.llm.unload()
            except Exception as exc:  # noqa: BLE001
                logger.warning("LM unload: {}", exc)
        self.dit = None
        self.llm = None
        gc.unfreeze()   # объекты прежних моделей снова доступны сборщику мусора
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def _download(self, plan: dict[str, Any], progress: ProgressCb) -> None:
        from acestep.model_downloader import ensure_dit_model, ensure_lm_model, ensure_main_model

        ckpt = config.CHECKPOINTS_DIR
        progress(0.02, "Проверка/загрузка моделей…")
        ok, msg = ensure_main_model(ckpt)
        if not ok:
            raise EngineError(f"Не удалось загрузить основную модель: {msg}")
        if plan["dit"] != "acestep-v15-turbo":
            ok, msg = ensure_dit_model(plan["dit"], ckpt)
            if not ok:
                raise EngineError(f"Не удалось загрузить {plan['dit']}: {msg}")
        if plan["lm"]:
            ok, msg = ensure_lm_model(plan["lm"], ckpt)
            if not ok:
                raise EngineError(f"Не удалось загрузить {plan['lm']}: {msg}")

    def ensure_loaded(self, settings: dict[str, Any], progress: ProgressCb, safe: bool = False) -> None:
        with self._lock:
            if self._fatal:
                raise EngineError(self._fatal)
            plan = resolve_plan(settings, safe=safe, loaded=self.state == "ready")
            if self.state == "ready" and self._requested == plan:
                return
            requested = dict(plan)
            self.state = "loading"
            self.last_error = None
            started = time.time()
            try:
                self._release()
                self._download(plan, progress)
                self._load(plan, progress)
            except Exception as exc:
                self.state = "error"
                self.last_error = str(exc)
                self._release()
                raise
            self.plan = plan
            self._requested = requested
            # Модели — десятки тысяч долгоживущих объектов: ACE вызывает gc.collect() на каждой генерации, и без
            # заморозки каждый вызов тратит ~0.3 с на их обход (3 вызова ≈ 1 с на трек).
            gc.collect()
            gc.freeze()
            self.load_seconds = round(time.time() - started, 1)
            logger.info("Модели загружены за {} с, план: {}", self.load_seconds, plan)
            self._warmup(progress)
            if not self._fatal:
                self.state = "ready"   # «готово» только после прогрева, чтобы статус не обманывал

    def _warmup(self, progress: ProgressCb) -> None:
        """Короткая холостая генерация: инициализирует ядра/аллокатор, чтобы первый настоящий трек не был медленнее."""
        self.warmup_seconds = None
        if (self.plan or {}).get("device") != "cuda":
            return
        from acestep.inference import GenerationConfig, GenerationParams, generate_music

        progress(0.9, "Прогрев моделей…")
        self._cancel.clear()
        t = time.time()
        try:
            turbo = "turbo" in self.plan["dit"]
            llm_ready = bool(self.llm is not None and getattr(self.llm, "llm_initialized", False))
            params = GenerationParams(
                caption="ambient, soft pads", lyrics="[Instrumental]", instrumental=True, vocal_language="unknown",
                duration=30, inference_steps=TURBO_STEPS if turbo else 8, shift=3.0,   # для прогрева хватит 8 шагов
                seed=1, thinking=llm_ready, use_cot_caption=False, use_cot_language=False,
            )
            cfg = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[1], audio_format="wav")
            res = generate_music(self.dit, self.llm, params, cfg, save_dir=None)
            if not res.success:
                logger.warning("Прогрев не удался: {}", res.error)
        except Exception as exc:  # noqa: BLE001 — прогрев необязателен
            logger.warning("Прогрев не удался: {}", exc)
            self._note_if_fatal(exc)
        self.warmup_seconds = round(time.time() - t, 1)
        logger.info("Прогрев завершён за {} с", self.warmup_seconds)

    @staticmethod
    def _looks_like_noise(result, allow_static: bool = False) -> str | None:
        """Быстрая проверка (без моделей, ~1 с). Возвращает причину брака или None.

        «шум»: белый шум имеет спектральную флэтность 0.5–1, музыка 0.001–0.05;
        «дрон»: длинный трек, где спектр почти не меняется (зациклившиеся коды LM) — признак вырождения.
        """
        from . import quality

        try:
            audio = result.audios[0]
            tensor = audio.get("tensor")
            if tensor is None:
                return None
            mono = tensor.float().mean(0).numpy()
            st = quality.spectral_stats(mono, int(audio.get("sample_rate") or 48000))
            if st["noise_ratio"] > NOISE_RATIO_LIMIT or st["flat_median"] > NOISE_FLAT_LIMIT:
                return f"шум (флэтность {st['flat_median']:.2f}, шумовых окон {st['noise_ratio']:.0%})"
            if not allow_static and mono.shape[0] / (audio.get("sample_rate") or 48000) >= 60 and st["static"] > STATIC_LIMIT:
                return f"статичный дрон ({st['static']:.0%} неизменных окон)"
        except Exception as exc:  # noqa: BLE001 — проверка не должна ронять генерацию
            logger.warning("Проверка на шум не удалась: {}", exc)
        return None

    @staticmethod
    def _chunk(left: int, duration: float, cover: bool | str = False) -> int:
        """Сколько кандидатов генерировать за один проход: не больше, чем безопасно по VRAM (та же формула, что у
        «VRAM guard» ACE) и чем помещается в быстрый LM-декодер.

        Кавер прожорливее: латенты исходника + вторая (не-cover) текстовая обусловленность. Замер (sft, 60 с):
        ~1.15 ГБ на вариант; с формулой ACE порция из 3 упиралась в потолок 16-ГБ карты, и Windows уводил память
        в общую ОЗУ — диффузия замедлялась в ~15 раз.
        """
        try:
            from acestep.gpu_config import get_effective_free_vram_gb

            per_sample = 1.2 * max(1.0, duration / 60.0) if cover else 0.5 + max(0.0, 0.15 * (duration - 60.0) / 60.0)
            if cover == "edit":
                per_sample *= 2   # FlowEdit: парные ветви (исходник и цель) на каждом шаге
            safe = max(1, int((get_effective_free_vram_gb() - 1.5) / per_sample))
        except Exception:  # noqa: BLE001 — нет CUDA/модуля: не ограничиваем
            safe = LM_BATCH_CHUNK
        return max(1, min(left, safe, LM_BATCH_CHUNK))

    @staticmethod
    def _scorer_device() -> str:
        """Оценщики (Audiobox ~0.5 ГБ, CLAP ~0.7 ГБ, Whisper ~1.7 ГБ) — на GPU, если есть запас; иначе на CPU."""
        try:
            import torch

            if torch.cuda.is_available() and torch.cuda.mem_get_info()[0] > 4.5 * 2**30:
                return "cuda"
        except Exception:  # noqa: BLE001
            pass
        return "cpu"

    @contextlib.contextmanager
    def _scoring_headroom(self):
        """Освобождает VRAM под оценщики: DiT, VAE и текстовый энкодер на время переезжают в ОЗУ (быстро по PCIe).

        LM не трогаем (его веса зашиты в CUDA Graph), он весит ~3.5 ГБ — с ним остаётся ~8 ГБ свободно.
        Только для режима без offload; в offload-режиме модели и так живут в ОЗУ.
        """
        plan = self.plan or {}
        parts = [] if plan.get("device") != "cuda" or plan.get("offload") or self.dit is None else \
            [m for m in (getattr(self.dit, "model", None), getattr(self.dit, "vae", None),
                         getattr(self.dit, "text_encoder", None)) if m is not None]
        moved = []
        try:
            if parts:
                import torch

                for m in parts:
                    m.to("cpu")
                    moved.append(m)
                torch.cuda.empty_cache()
            yield
        finally:
            for m in moved:
                m.to(getattr(self.dit, "device", "cuda"))

    def _rank(self, tracks: list[dict[str, Any]], lyrics: str, instrumental: bool, caption: str,
              progress: ProgressCb, src: str | None = None) -> None:
        """Оценивает кандидатов и сортирует: лучший первым.

        Сводный балл: эстетика Audiobox (+ штрафы за шум/статику), соответствие описанию (CLAP),
        для вокала — разборчивость русского текста (Whisper); для кавера (src) — узнаваемость оригинала.
        """
        from . import quality

        sing = not instrumental and bool(lyrics.strip()) and lyrics.strip() != "[Instrumental]"
        n = len(tracks)
        try:
            with self._scoring_headroom():
                device = self._scorer_device()
                for i, track in enumerate(tracks):
                    self.check_cancel()
                    progress(0.9 + 0.08 * i / n, f"Оценка варианта {i + 1}/{n}…")
                    aes = quality.score_file(track["path"], device=device)
                    clap = quality.clap_similarity(track["path"], caption, device=device)
                    lyr = None
                    if sing:
                        lyr = quality.lyrics_match(quality.transcribe(track["path"], device=device), lyrics)
                    retention = quality.chroma_similarity(src, track["path"]) if src else None
                    total = quality.total_score(aes, lyr, clap) if src is None                         else quality.cover_score(aes, clap, retention, lyr)
                    track["score"] = {
                        "total": round(total, 3),
                        "clap": round(clap, 3),
                        **({"retention": round(retention, 3)} if retention is not None else {}),
                        **{k: round(float(aes[k]), 3) for k in ("CE", "PQ", "CE_p10", "noise_ratio", "static")},
                        **({k: round(v, 3) for k, v in lyr.items()} if lyr else {}),
                    }
        finally:
            quality.release()
            quality.release_whisper()
            quality.release_clap()
        tracks.sort(key=lambda t: -t["score"]["total"])
        for rank, track in enumerate(tracks, 1):
            track["score"]["rank"] = rank
        logger.info("Ранжирование: {}", [t["score"]["total"] for t in tracks])

    def _note_if_fatal(self, exc: BaseException) -> bool:
        """Фатальная ошибка CUDA необратима в рамках процесса — запоминаем и просим перезапуск."""
        text = f"{type(exc).__name__} {exc}".lower()
        if any(marker in text for marker in _FATAL_CUDA) and not _is_oom(exc):
            self._fatal = ("Критическая ошибка CUDA — контекст видеокарты повреждён. "
                           "Закройте окно сервера и запустите run.bat заново.")
            self.state, self.last_error = "error", self._fatal
            logger.error("Фатальная ошибка CUDA: {}", exc)
            return True
        return False

    def _load(self, plan: dict[str, Any], progress: ProgressCb) -> None:
        import torch

        from acestep.handler import AceStepHandler

        from .lmhandler import FastLLMHandler

        if plan["device"] == "cpu":
            torch.set_num_threads(hardware()["cores"] or os.cpu_count() or 8)
        elif not plan["offload"] and hardware()["gpus"][0]["vram_gb"] >= 15:
            # ACE по умолчанию декодирует VAE мелкими тайлами (128), если свободно < 12 ГБ; на 16-ГБ карте без offload
            # тайл 512 влезает (пик +1.1 ГБ) и декодирует на ~25% быстрее.
            os.environ.setdefault("ACESTEP_VAE_DECODE_CHUNK_SIZE", "512")

        progress(0.05, f"Загрузка DiT ({plan['dit']}) на {plan['device'].upper()}…")
        dit = AceStepHandler()
        msg, ok = dit.initialize_service(
            project_root=str(config.ACE_ROOT),
            config_path=plan["dit"],
            device=plan["device"],
            use_flash_attention=True,
            compile_model=False,
            offload_to_cpu=plan["offload"],
            offload_dit_to_cpu=False,
            quantization=plan["quant"],
        )
        if not ok:
            raise EngineError(f"DiT не загрузился: {msg}")
        self.dit = dit

        self.llm = None
        if plan["lm"]:
            progress(0.15, f"Загрузка LM ({plan['lm']})…")
            llm = FastLLMHandler()
            llm.fast_enabled = plan.get("fast_lm", True)
            llm.cancel_check = self.check_cancel   # проверка отмены на каждом токене
            backends = [plan["backend"]] + (["pt"] if plan["backend"] != "pt" else [])
            for backend in backends:
                msg, ok = llm.initialize(
                    checkpoint_dir=str(config.CHECKPOINTS_DIR),
                    lm_model_path=plan["lm"],
                    backend=backend,
                    device=plan["device"],
                    offload_to_cpu=plan["offload"],
                    dtype=None,
                )
                if ok:
                    # апстрим сам откатывается vllm → pt (нет Triton под Windows): пишем реальный бэкенд
                    plan["backend"] = getattr(llm, "llm_backend", None) or backend
                    plan["fast_lm"] = getattr(llm, "fast", None) is not None   # реально включился ли ускоренный декодер
                    self.llm = llm
                    break
                logger.warning("LM backend {} не поднялся: {}", backend, msg)
            if self.llm is None:
                logger.warning("LM недоступна — работаю без планировщика")
                plan["lm"] = None

    # ---- генерация ----
    def generate(self, req: dict[str, Any], settings: dict[str, Any], progress: ProgressCb,
                 out_dir: Path) -> dict[str, Any]:
        """Выполняет задачу; возвращает {"tracks": [...], "caption", "lyrics", ...}."""
        self._cancel.clear()
        try:
            return self._dispatch(req, settings, progress, out_dir, safe=False)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._note_if_fatal(exc):
                raise EngineError(self._fatal) from exc
            if _is_oom(exc) and (self.plan or {}).get("device") == "cuda":
                logger.warning("Нехватка VRAM — перезагружаю модели в экономном режиме")
                self.unload(reset_safe=False)
                self._safe_mode = True   # остаёмся в нём, пока пользователь не нажмёт «Выгрузить»
                return self._dispatch(req, settings, progress, out_dir, safe=True)
            raise

    def _dispatch(self, req: dict[str, Any], settings: dict[str, Any], progress: ProgressCb,
                  out_dir: Path, safe: bool) -> dict[str, Any]:
        if req.get("task") == "cover":
            return self._generate_cover(req, settings, progress, out_dir, safe)
        return self._generate(req, settings, progress, out_dir, safe)

    # ---- каверы ----
    def _understand(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """Описание исходника от LM (жанр, инструменты, текст): нужно FlowEdit как «откуда» перекрашивать.

        Исходник → семантические коды (токенайзер DiT) → understand_music. ~5 с; кэш по фрагменту.
        """
        if not (self.llm is not None and getattr(self.llm, "llm_initialized", False)):
            return None
        key = (req.get("source_id") or req.get("source_track_id") or req["_src_path"],
               round(float(req.get("start") or 0), 2), req.get("end"))
        if key in self._understood:
            return self._understood[key]
        from acestep.inference import understand_music

        codes = self.dit.convert_src_audio_to_codes(req["_src_path"]) or ""
        if "<|audio_code_" not in codes:
            logger.warning("Исходник не перевёлся в коды: {}", codes[:200])
            return None
        self.check_cancel()
        res = understand_music(self.llm, codes, use_constrained_decoding=True)
        self.check_cancel()
        if not res.success or not (res.caption or "").strip():
            logger.warning("LM не описала исходник: {}", res.error or res.status_message)
            return None
        und = {"caption": res.caption.strip(), "lyrics": (res.lyrics or "").strip(),
               "bpm": res.bpm, "keyscale": res.keyscale}
        if len(self._understood) > 32:
            self._understood.clear()
        self._understood[key] = und
        logger.info("Исходник кавера: {}", und["caption"][:200])
        return und

    def _generate_cover(self, req: dict[str, Any], settings: dict[str, Any], progress: ProgressCb,
                        out_dir: Path, safe: bool) -> dict[str, Any]:
        """Кавер двумя способами, лучший выбирается оценкой (замеры: scripts/cover_lab.py).

        «По нотам» (task cover): DiT получает семантические коды исходника. Хорошо, когда исходник «понятен»
        модели (напр. сгенерирован ей же): уверенная смена стиля при узнаваемой гармонии. На живых сложных записях
        (оркестр) коды противоречат новому стилю — получается каша без мелодии оригинала.
        «Перекраска» (FlowEdit): исходник плавно переводится из своего описания (его даёт LM) в целевое — чистый звук
        и сохранённая гармония на живых записях, но на «родных» исходниках стиль меняется слабо.
        Авто: часть вариантов каждым способом; ранжирование учитывает и узнаваемость оригинала.
        """
        with self._lock:
            t0 = time.time()
            self.ensure_loaded(settings, progress, safe=safe or self._safe_mode)
            mode = req.get("cover_mode") if req.get("cover_mode") in ("auto", "cover", "edit") else "auto"
            batch = max(1, min(MAX_BATCH, int(req.get("batch", 1))))
            und = None
            if mode in ("auto", "edit"):
                progress(0.03, "LM слушает исходник…")
                und = self._understand(req)
                if und is None:
                    if mode == "edit":
                        raise EngineError("Для «перекраски» нужна языковая модель (LM): она описывает исходник. "
                                          "Включите её в настройках или выберите способ «По нотам»")
                    mode = "cover"
            if mode == "auto":
                n = max(2, batch)   # хотя бы по одному варианту каждым способом
                plan = [("cover", n - n // 2), ("edit", n // 2)]
            else:
                plan = [(mode, batch)]
            # «Близость к оригиналу» для перекраски: окно FlowEdit [0, n_max], меньше n_max — ближе к исходнику
            closeness = float(req.get("cover_strength", 0.6))
            n_max = req.get("flow_n_max")
            n_max = float(n_max) if n_max is not None else min(1.0, max(0.6, 1.16 - 0.4 * closeness))

            labels = {"cover": "По нотам", "edit": "Перекраска"}
            total, done = sum(k for _, k in plan), 0
            parts, tracks = [], []
            for method, k in plan:
                sub = {**req, "cover_mode": method, "batch": k, "rank": k > 1, "_skip_rank": True, "keep_all": True}
                if method == "edit":
                    sub.update(source_caption=und["caption"], source_lyrics=und["lyrics"] or "[Instrumental]",
                               flow_n_max=n_max)
                a, b = done / total, (done + k) / total

                def sub_progress(v: float, desc: str, _a=a, _b=b, _m=method) -> None:
                    progress(0.05 + 0.83 * (_a + (_b - _a) * v), f"{labels[_m]}: {desc}")

                res = self._generate(sub, settings, sub_progress, out_dir / method, safe=safe)
                for track in res["tracks"]:
                    track["method"] = method
                    track["generation"] = res.get("generation")   # у способов разные конфиги — храним свой у каждого
                tracks += res["tracks"]
                parts.append(res)
                done += k

            first = parts[0]
            ranked = False
            if len(tracks) > 1:
                t_rank = time.time()
                lyrics = first["lyrics"]
                self._rank(tracks, lyrics, lyrics == "[Instrumental]", first["caption"], progress, src=req["_src_path"])
                ranked = True
                if not req.get("keep_all"):
                    for extra_track in tracks[1:]:
                        Path(extra_track["path"]).unlink(missing_ok=True)
                    tracks = tracks[:1]
                rank_seconds = round(time.time() - t_rank, 2)
            stages: dict[str, float] = {}
            for part in parts:
                for k, v in (part.get("stages") or {}).items():
                    stages[k] = round(stages.get(k, 0) + v, 2)
            if ranked:
                stages["rank"] = rank_seconds
            stages["total"] = round(time.time() - t0, 2)
            return {**first, "tracks": tracks, "ranked": ranked, "stages": stages,
                    "gen_seconds": round(time.time() - t0, 1),
                    "cover": {"mode": req.get("cover_mode") or "auto", "methods": [m for m, _ in plan],
                              "flow_n_max": round(n_max, 3), "closeness": closeness,
                              "source_caption": und["caption"] if und else None,
                              "source_understanding": _jsonable(und) if und else None}}

    def _generate(self, req: dict[str, Any], settings: dict[str, Any], progress: ProgressCb,
                  out_dir: Path, safe: bool) -> dict[str, Any]:
        from acestep.inference import GenerationConfig, GenerationParams, create_sample, generate_music

        with self._lock:
            t0 = time.time()
            stages: dict[str, float] = {}
            progress(0.01, "Подготовка запроса…")
            prompt = (req.get("prompt") or "").strip()
            description_en = translate.ru_to_en(prompt) if settings.get("translate", True) else prompt
            stages["translate"] = round(time.time() - t0, 2)
            caption, negative = presets.build_caption(
                req.get("genres") or [], req.get("moods") or [], req.get("vocal") or "auto",
                description_en, req.get("negative_genres") or [],
            )
            if not caption:
                caption = "instrumental music" if presets.is_instrumental(req.get("vocal", "")) else "music"

            self.ensure_loaded(settings, progress, safe=safe or self._safe_mode)
            llm_ready = bool(self.llm is not None and getattr(self.llm, "llm_initialized", False))
            turbo = "turbo" in self.plan["dit"]

            cover = req.get("task") == "cover"
            # Способ кавера: "cover" — DiT на семантических кодах исходника (+ старт с зашумлённого исходника);
            # "edit" — FlowEdit поверх text2music: исходник «перекрашивается» из своего описания в целевое.
            edit_mode = cover and req.get("cover_mode") == "edit"
            lyrics = (req.get("lyrics") or "").strip()
            instrumental = (presets.is_instrumental(req.get("vocal") or "auto") or bool(req.get("instrumental"))
                            or (cover and not lyrics))   # кавер без текста — инструментал
            temperature = float(req.get("temperature") if req.get("temperature") is not None else 0.85)
            lm_sample: dict[str, Any] = {}
            language = req.get("vocal_language") or "ru"
            if instrumental:
                lyrics = "[Instrumental]"
            elif not lyrics and req.get("auto_lyrics", True) and llm_ready and not cover:
                progress(0.2, "LM пишет текст песни…")
                # LM иногда вместо текста отдаёт одни теги ([Instrumental], [Intro]…) — тогда вокала не будет; повторяем
                for attempt in range(1 + LYRICS_RETRIES):
                    sample = create_sample(self.llm, query=_with_language(caption, language), instrumental=False,
                                           vocal_language=language, temperature=temperature)
                    if not (sample.success and sample.lyrics) or _has_sung_lines(sample.lyrics):
                        break
                    logger.info("LM не написала текст (попытка {}), повторяю", attempt + 1)
                if sample.success and sample.lyrics:
                    lyrics = sample.lyrics
                    lm_sample = {"bpm": sample.bpm, "keyscale": sample.keyscale}
                else:
                    logger.warning("create_sample: {}", sample.error or sample.status_message)

            steps = req.get("steps") or (TURBO_STEPS if turbo else EDIT_STEPS if edit_mode else QUALITY_STEPS)
            seed = req.get("seed")
            use_random = seed is None or int(seed) < 0
            user_random = use_random
            batch = max(1, min(MAX_BATCH, int(req.get("batch", 1))))
            rank_mode = bool(req.get("rank")) and batch > 1
            # Одиночный трек: seed выбираем сами и им же сеем LM (ACE в одиночном режиме генератор LM не сеет) —
            # тогда сохранённый seed повторяет трек целиком: и коды LM, и шум диффузии.
            pin_seed = batch == 1
            if pin_seed and use_random:
                seed, use_random = random.randrange(2**32), False
            duration = float(req.get("duration", 60))
            bpm = req.get("bpm") or lm_sample.get("bpm") or None

            # Дополнительные ручки качества (все необязательны; значения по умолчанию — рекомендованные ACE)
            extra: dict[str, Any] = {}
            for key, attr in (("adg", "use_adg"), ("lm_top_p", "lm_top_p"), ("lm_top_k", "lm_top_k"),
                              ("cfg_start", "cfg_interval_start"), ("cfg_end", "cfg_interval_end"),
                              ("infer_method", "infer_method"), ("timesignature", "timesignature"),
                              ("sampler", "sampler_mode"), ("dcw", "dcw_enabled"), ("dcw_scaler", "dcw_scaler"),
                              ("vel_norm", "velocity_norm_threshold"), ("vel_ema", "velocity_ema_factor")):
                if req.get(key) not in (None, ""):
                    extra[attr] = req[key]
            # «SDE» в выборе сэмплера — это метод интегрирования, а не sampler_mode: Heun с SDE ACE не совмещает
            if extra.get("sampler_mode") == "sde":
                extra.update(sampler_mode="euler", infer_method="sde")

            if cover:
                # Мелодию, ритм и структуру задаёт исходник; LM для cover ACE пропускает сам. Инструкцию задачи
                # ACE подставляет только при audio_codes — для src_audio передаём её явно.
                from acestep.constants import TASK_INSTRUCTIONS

                extra["src_audio"] = req["_src_path"]
                if edit_mode:
                    extra.update(
                        flow_edit_morph=True,
                        flow_edit_source_caption=req.get("source_caption") or "",
                        flow_edit_source_lyrics=req.get("source_lyrics") or "[Instrumental]",
                        flow_edit_n_min=float(req.get("flow_n_min") or 0.0),
                        flow_edit_n_max=float(req.get("flow_n_max") if req.get("flow_n_max") is not None else 1.0),
                        flow_edit_n_avg=int(req.get("flow_n_avg") or 1),
                    )
                else:
                    extra.update(
                        instruction=TASK_INSTRUCTIONS["cover"],
                        audio_cover_strength=float(req.get("cover_strength", 0.5)),
                        cover_noise_strength=float(req.get("cover_noise") or 0.0),
                    )

            params = GenerationParams(
                task_type="cover" if cover and not edit_mode else "text2music",
                caption=caption,
                lyrics=lyrics,
                instrumental=instrumental,
                vocal_language="unknown" if instrumental else language,
                bpm=int(bpm) if bpm else None,
                keyscale=req.get("keyscale") or lm_sample.get("keyscale") or "",
                duration=duration,
                inference_steps=int(steps),
                guidance_scale=float(req.get("guidance") or 7.0),
                shift=float(req.get("shift") or 3.0),
                seed=-1 if use_random else int(seed),
                thinking=bool(req.get("thinking", True)) and llm_ready and not cover,
                lm_temperature=temperature,
                lm_cfg_scale=float(req.get("lm_cfg_scale") or LM_CFG_DEFAULT),
                lm_negative_prompt=negative,
                # LM переписывает caption в «родной» для DiT формат (на таких описаниях DiT обучался)
                use_cot_caption=bool(req.get("cot_caption", True)) and not cover and not presets.literal_caption(req.get("genres") or []),
                use_cot_language=False,
                use_cot_metas=bool(req.get("cot_metas", True)) and not cover,
                **extra,
            )
            out_format = req.get("format") or settings["audio_format"]
            if rank_mode:
                use_random = True    # кандидаты должны различаться
            cfg = GenerationConfig(
                batch_size=self._chunk(batch, duration, "edit" if edit_mode else cover) if rank_mode else batch,
                allow_lm_batch=batch > 1,
                lm_batch_chunk_size=LM_BATCH_CHUNK,   # ≤4 элементов (8 строк) за проход LM — укладывается в быстрый декодер
                use_random_seed=use_random,
                seeds=None if use_random else [int(seed)],
                # MP3 в ACE-Step требует ffmpeg.exe — сохраняем FLAC и перекодируем сами (audio.convert_to_mp3)
                audio_format="flac" if out_format == "mp3" else out_format,
            )

            floor = 0.25

            def cb(value: float = 0.0, desc: str = "", *_a: Any, **_k: Any) -> None:
                self.check_cancel()
                try:
                    progress(min(0.98, floor + (1 - floor) * float(value)), str(desc or "Генерация…"))
                except Exception:  # noqa: BLE001
                    pass

            if self.llm is not None and hasattr(self.llm, "codes_rep_penalty"):
                self.llm.codes_rep_penalty = float(req.get("lm_rep_penalty") or 1.0)

            out_dir.mkdir(parents=True, exist_ok=True)
            self.check_cancel()
            progress(floor, "Генерация музыки…")
            t_gen = time.time()
            stages["prepare"] = round(t_gen - t0 - stages["translate"], 2)
            # Страж против «каши»: для одиночного трека со случайным seed проверяем спектр и при шуме пробуем ещё раз.
            retries = MAX_NOISE_RETRIES if (req.get("guard", True) and batch == 1 and user_random) else 0
            ambient_ok = bool({"ambient", "lofi", "classical"} & set(req.get("genres") or []))   # там статика — норма
            attempt = 0
            while True:
                if pin_seed:
                    _seed_lm(int(seed))
                result = generate_music(self.dit, self.llm, params, cfg, save_dir=str(out_dir), progress=cb)
                self.check_cancel()   # ACE глотает исключения внутри цикла LM и возвращает «ошибку» — различаем отмену
                if not result.success:
                    raise EngineError(result.error or result.status_message or "Генерация не удалась")
                reason = None if attempt >= retries else self._looks_like_noise(result, allow_static=ambient_ok)
                if reason is None:
                    break
                attempt += 1
                logger.warning("Брак: {} — перегенерирую ({}/{})", reason, attempt, retries)
                progress(floor, f"Результат неудачный ({reason.split(' (')[0]}) — пробую ещё раз ({attempt}/{retries})…")
                for a in result.audios:
                    Path(a.get("path") or "").unlink(missing_ok=True)
                seed = random.randrange(2**32)   # заново — с новым seed
                params.seed, cfg.seeds = seed, [seed]
            audios = list(result.audios)
            # Недостающих кандидатов догенерируем порциями, посильными для свободной VRAM (LM для каждой порции
            # считается заново, зато ничего не выбрасывается).
            extra_calls = 0
            while rank_mode and len(audios) < batch and extra_calls < MAX_EXTRA_BATCH_CALLS:
                cfg.batch_size = self._chunk(batch - len(audios), duration, "edit" if edit_mode else cover)
                extra_calls += 1
                progress(floor, f"Догенерирую варианты ({len(audios)}/{batch})…")
                more = generate_music(self.dit, self.llm, params, cfg, save_dir=str(out_dir), progress=cb)
                self.check_cancel()
                if not more.success or not more.audios:
                    logger.warning("Догенерация вариантов не удалась: {}", more.error)
                    break
                audios += more.audios
            stages["generate_music"] = round(time.time() - t_gen, 2)
            stages["noise_retries"] = attempt
            stages["extra_batch_calls"] = extra_calls

            tracks = []
            for audio in audios:
                if not audio.get("path"):
                    raise EngineError("ACE-Step не сохранил аудиофайл (подробности в логе сервера)")
                tensor = audio.get("tensor")
                sr = audio.get("sample_rate") or 48000
                dur = float(tensor.shape[-1]) / sr if tensor is not None else duration
                tracks.append({
                    "path": audio["path"],
                    "seed": (audio.get("params") or {}).get("seed"),
                    "duration": round(dur, 2),
                    "ace": _jsonable(audio.get("params") or {}, drop=_ACE_DROP),   # всё, что ушло в ACE для этого трека
                })

            ranked = False
            if len(tracks) > 1 and req.get("rank") and not req.get("_skip_rank"):
                t_rank = time.time()
                self._rank(tracks, lyrics, instrumental, caption, progress)
                stages["rank"] = round(time.time() - t_rank, 2)
                ranked = True
                if not req.get("keep_all"):
                    for extra_track in tracks[1:]:
                        Path(extra_track["path"]).unlink(missing_ok=True)
                    tracks = tracks[:1]

            if out_format == "mp3":
                for track in tracks:
                    progress(0.99, "Кодирование MP3…")
                    track["path"] = str(convert_to_mp3(Path(track["path"])))
            stages["total"] = round(time.time() - t0, 2)
            return {
                "stages": stages,
                "ranked": ranked,
                "tracks": tracks,
                "caption": caption,
                "description_en": description_en,
                "negative": negative,
                "lyrics": lyrics,
                "gen_seconds": round(time.time() - t0, 1),
                "plan": dict(self.plan or {}),
                "time_costs": (result.extra_outputs or {}).get("time_costs", {}),
                "generation": {
                    "lm_metadata": _jsonable((result.extra_outputs or {}).get("lm_metadata") or {}),
                    "config": _jsonable(cfg.to_dict()),
                    # одиночная генерация с seed, которым посеяна и LM: тот же запрос + seed дают тот же трек.
                    # Варианты пакета так не повторить — LM и общие шаги пакета ACE считает иначе, чем одиночный трек
                    "reproducible": pin_seed,
                    "lm_codes_rep_penalty": float(req.get("lm_rep_penalty") or 1.0),
                    "out_format": out_format,
                    "lm_sample": _jsonable(lm_sample),
                    "settings": _jsonable(settings),
                },
            }


# Тяжёлые или бесполезные для повтора поля параметров ACE: LM-коды (десятки КБ), путь к временному исходнику
_ACE_DROP = frozenset({"audio_codes", "src_audio", "reference_audio"})
_SKIP = object()


def _jsonable(value: Any, drop: frozenset[str] = frozenset()) -> Any:
    """Приводит параметры к JSON: тензоры и прочие объекты отбрасываются, numpy/torch-скаляры → числа."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k) not in drop and (v := _jsonable(v)) is not _SKIP:
                out[str(k)] = v
        return out
    if isinstance(value, (list, tuple)):
        return [v for v in map(_jsonable, value) if v is not _SKIP]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if getattr(value, "ndim", None) == 0 and hasattr(value, "item"):
        return value.item()
    return _SKIP


def _with_language(caption: str, language: str) -> str:
    """Запрос на текст песни с явным языком: ACE ограничивает язык только в метаданных, а сам текст LM пишет как хочет."""
    name = LYRICS_LANGUAGES.get(language)
    return f"{caption}, {name} lyrics" if name else caption


def _has_sung_lines(lyrics: str) -> bool:
    """Есть ли в тексте хоть одна строка, которую поют (не тег секции вроде [Verse] или [Instrumental])."""
    return any(line.strip() and not line.strip().startswith("[") for line in (lyrics or "").splitlines())


def _seed_lm(seed: int) -> None:
    """Сеет глобальный генератор torch: от него сэмплирует LM (и CoT-метаданные, и аудиокоды)."""
    import torch

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "out of memory" in text or "outofmemory" in text


_engine: AceEngine | None = None


def get_engine() -> AceEngine:
    global _engine
    if _engine is None:
        _engine = AceEngine()
    return _engine
