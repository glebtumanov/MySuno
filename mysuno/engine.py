"""Движок ACE-Step 1.5: определение железа, загрузка моделей, генерация."""
from __future__ import annotations

import contextlib
import gc
import os
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
MAX_NOISE_RETRIES = 2
NOISE_RATIO_LIMIT = 0.25   # доля 10-с окон с флэтностью > 0.15, после которой результат считаем «кашей»
NOISE_FLAT_LIMIT = 0.10    # или медианная флэтность по треку
STATIC_LIMIT = 0.60        # доля почти неизменных соседних 10-с окон = «дрон» (у нормальных треков < 0.3)
LM_BATCH_CHUNK = 4         # элементов за проход LM при батче (с CFG = 8 строк → быстрый декодер)
MAX_EXTRA_BATCH_CALLS = 4  # сколько раз догенерировать недостающих кандидатов
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

        free, total = torch.cuda.mem_get_info(0)
        info["gpus"][0]["free_gb"] = round(free / 2**30, 1)
        info["gpus"][0]["vram_gb"] = round(total / 2**30, 1)
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
    def _chunk(left: int, duration: float) -> int:
        """Сколько кандидатов генерировать за один проход: не больше, чем безопасно по VRAM (та же формула, что у
        «VRAM guard» ACE) и чем помещается в быстрый LM-декодер."""
        try:
            from acestep.gpu_config import get_effective_free_vram_gb

            per_sample = 0.5 + max(0.0, 0.15 * (duration - 60.0) / 60.0)
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
              progress: ProgressCb) -> None:
        """Оценивает кандидатов и сортирует: лучший первым.

        Сводный балл: эстетика Audiobox (+ штрафы за шум/статику), соответствие описанию (CLAP),
        для вокала — разборчивость русского текста (Whisper).
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
                    track["score"] = {
                        "total": round(quality.total_score(aes, lyr, clap), 3),
                        "clap": round(clap, 3),
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
            return self._generate(req, settings, progress, out_dir, safe=False)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._note_if_fatal(exc):
                raise EngineError(self._fatal) from exc
            if _is_oom(exc) and (self.plan or {}).get("device") == "cuda":
                logger.warning("Нехватка VRAM — перезагружаю модели в экономном режиме")
                self.unload(reset_safe=False)
                self._safe_mode = True   # остаёмся в нём, пока пользователь не нажмёт «Выгрузить»
                return self._generate(req, settings, progress, out_dir, safe=True)
            raise

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

            instrumental = presets.is_instrumental(req.get("vocal") or "auto") or bool(req.get("instrumental"))
            lyrics = (req.get("lyrics") or "").strip()
            temperature = float(req.get("temperature") if req.get("temperature") is not None else 0.85)
            lm_sample: dict[str, Any] = {}
            if instrumental:
                lyrics = "[Instrumental]"
            elif not lyrics and req.get("auto_lyrics", True) and llm_ready:
                progress(0.2, "LM пишет текст песни…")
                sample = create_sample(self.llm, query=caption, instrumental=False,
                                       vocal_language="ru", temperature=temperature)
                if sample.success and sample.lyrics:
                    lyrics = sample.lyrics
                    lm_sample = {"bpm": sample.bpm, "keyscale": sample.keyscale}
                else:
                    logger.warning("create_sample: {}", sample.error or sample.status_message)

            steps = req.get("steps") or (TURBO_STEPS if turbo else QUALITY_STEPS)
            seed = req.get("seed")
            use_random = seed is None or int(seed) < 0
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

            params = GenerationParams(
                task_type="text2music",
                caption=caption,
                lyrics=lyrics,
                instrumental=instrumental,
                vocal_language="unknown" if instrumental else "ru",
                bpm=int(bpm) if bpm else None,
                keyscale=req.get("keyscale") or lm_sample.get("keyscale") or "",
                duration=duration,
                inference_steps=int(steps),
                guidance_scale=float(req.get("guidance") or 7.0),
                shift=float(req.get("shift") or 3.0),
                seed=-1 if use_random else int(seed),
                thinking=bool(req.get("thinking", True)) and llm_ready,
                lm_temperature=temperature,
                lm_cfg_scale=float(req.get("lm_cfg_scale") or LM_CFG_DEFAULT),
                lm_negative_prompt=negative,
                # LM переписывает caption в «родной» для DiT формат (на таких описаниях DiT обучался)
                use_cot_caption=bool(req.get("cot_caption", True)),
                use_cot_language=False,
                use_cot_metas=bool(req.get("cot_metas", True)),
                **extra,
            )
            batch = max(1, min(8, int(req.get("batch", 1))))
            out_format = req.get("format") or settings["audio_format"]
            rank_mode = bool(req.get("rank")) and batch > 1
            if rank_mode:
                use_random = True    # кандидаты должны различаться
            cfg = GenerationConfig(
                batch_size=self._chunk(batch, duration) if rank_mode else batch,
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
            retries = MAX_NOISE_RETRIES if (req.get("guard", True) and batch == 1 and use_random) else 0
            ambient_ok = bool({"ambient", "lofi", "classical"} & set(req.get("genres") or []))   # там статика — норма
            attempt = 0
            while True:
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
            audios = list(result.audios)
            # Недостающих кандидатов догенерируем порциями, посильными для свободной VRAM (LM для каждой порции
            # считается заново, зато ничего не выбрасывается).
            extra_calls = 0
            while rank_mode and len(audios) < batch and extra_calls < MAX_EXTRA_BATCH_CALLS:
                cfg.batch_size = self._chunk(batch - len(audios), duration)
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
                })

            ranked = False
            if len(tracks) > 1 and req.get("rank"):
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
            }


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "out of memory" in text or "outofmemory" in text


_engine: AceEngine | None = None


def get_engine() -> AceEngine:
    global _engine
    if _engine is None:
        _engine = AceEngine()
    return _engine
