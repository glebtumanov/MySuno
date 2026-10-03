"""Объективная оценка качества аудио без эталона: Audiobox Aesthetics (Meta, обучена на оценках людей) + эвристики шума.

Оси Audiobox (шкала 0–10): PQ — качество продакшна, PC — сложность, CE — «приятность» содержания, CU — полезность.
Предиктор оценивает 10-секундные окна; мы считаем среднее и худшее окно (ловит провалы посреди трека).
Эвристики: `noise_ratio` — доля окон с высокой спектральной флэтностью («каша из шума»), `static` — доля почти
неизменных соседних окон (зацикленный дрон). Предиктор — модель Meta (CC-BY 4.0), грузится с Hugging Face при первом вызове.
"""
from __future__ import annotations

import difflib
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

WINDOW_S = 10.0
_predictor = None
_device = None
_lock = threading.Lock()


def _get_predictor(device: str = "cuda"):
    """device: "cuda" | "cpu". Предиктор по умолчанию встаёт на GPU; для параллельной работы с генерацией — на CPU."""
    global _predictor, _device
    import torch

    want = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
    with _lock:
        if _predictor is None:
            from audiobox_aesthetics.infer import initialize_predictor

            _predictor = initialize_predictor()
        if _device != want:
            _predictor.model.to(want)
            _predictor.device = torch.device(want)
            _device = want
        return _predictor


def release() -> None:
    """Выгружает предиктор (освобождает VRAM)."""
    global _predictor, _device
    with _lock:
        _predictor, _device = None, None
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- эвристики
def _mel_filter(n_freq: int, sr: int, n_mels: int = 48, fmin: float = 60.0, fmax: float = 16000.0) -> np.ndarray:
    def hz2mel(f):
        return 2595 * np.log10(1 + f / 700)

    def mel2hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    freqs = np.linspace(0, sr / 2, n_freq)
    fb = np.zeros((n_mels, n_freq), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.clip(np.minimum((freqs - lo) / (mid - lo + 1e-9), (hi - freqs) / (hi - mid + 1e-9)), 0, None)
    return fb


def spectral_stats(mono: np.ndarray, sr: int, window_s: float = WINDOW_S) -> dict[str, float]:
    """Флэтность по окнам (белый шум ≈ 0.5–1, музыка ≈ 0.001–0.05), доля ВЧ-энергии, «статичность» соседних окон."""
    n_fft, hop = 2048, 1024
    if len(mono) < n_fft * 4:
        return {"flat_median": 0.0, "flat_max": 0.0, "noise_ratio": 0.0, "hf": 0.0, "static": 0.0}
    win = np.hanning(n_fft).astype(np.float32)
    n = 1 + (len(mono) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(mono[idx] * win, axis=1)) ** 2 + 1e-12
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = (freqs >= 100) & (freqs <= 16000)
    s = spec[:, band]
    flat = np.exp(np.log(s).mean(1)) / s.mean(1)
    hf = float((spec[:, freqs > 8000].sum(1) / spec.sum(1)).mean())
    per = max(1, int(window_s * sr / hop))
    starts = [i for i in range(0, len(flat), per) if len(flat) - i >= per // 2]     # хвостовой огрызок < полокна не берём
    wins = [float(np.median(flat[i:i + per])) for i in starts] or [float(np.median(flat))]
    # статичность: косинусная близость усреднённых лог-мел-спектров соседних окон
    mel = np.log(spec @ _mel_filter(spec.shape[1], sr).T + 1e-9)
    vecs = np.stack([mel[i:i + per].mean(0) for i in starts]) if len(starts) > 1 else None
    static = 0.0
    if vecs is not None:
        vecs = vecs - vecs.mean(1, keepdims=True)
        v = vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
        sims = (v[:-1] * v[1:]).sum(1)
        static = float(np.mean(sims > 0.995))
    return {"flat_median": float(np.median(wins)), "flat_max": float(np.max(wins)),
            "noise_ratio": float(np.mean([w > 0.15 for w in wins])), "hf": hf, "static": static}


def score_file(path: str | Path, device: str = "cuda") -> dict[str, Any]:
    """Оценивает файл: среднее и худшее по 10-с окнам PQ/PC/CE/CU + эвристики шума/статики."""
    import torch

    wav, sr = sf.read(str(path), dtype="float32", always_2d=True)       # [T, C]
    tensor = torch.from_numpy(wav.T.copy())                              # [C, T]
    predictor = _get_predictor(device)
    seg = int(WINDOW_S * sr)
    chunks = [tensor[:, i:i + seg] for i in range(0, tensor.shape[1], seg) if tensor.shape[1] - i >= seg // 2]
    if not chunks:
        chunks = [tensor]
    outs: list[dict[str, float]] = []
    for i in range(0, len(chunks), 12):                                   # порциями — чтобы не раздувать память
        outs += predictor.forward([{"path": c, "sample_rate": sr} for c in chunks[i:i + 12]])
    res: dict[str, Any] = {k: float(np.mean([o[k] for o in outs])) for k in ("PQ", "PC", "CE", "CU")}
    ce = np.array([o["CE"] for o in outs])
    res["CE_min"] = float(ce.min())
    res["CE_p10"] = float(np.percentile(ce, 10))
    res.update(spectral_stats(wav.mean(1), sr))
    res["seconds"] = round(wav.shape[0] / sr, 1)
    res["windows"] = len(chunks)
    return res


def overall(s: dict[str, Any]) -> float:
    """Сводный балл для выбора лучшего варианта: CE и PQ, нижний дециль окон, штрафы за шум и статику."""
    score = 0.45 * s["CE"] + 0.25 * s["PQ"] + 0.30 * s["CE_p10"]
    score -= 4.0 * s["noise_ratio"] + 1.0 * s.get("static", 0.0)
    return float(score)


# ---------------------------------------------------------------- соответствие описанию (CLAP)
CLAP_ID = "laion/clap-htsat-unfused"
_clap = None


def _get_clap(device: str):
    global _clap
    from transformers import ClapModel, ClapProcessor

    with _lock:
        if _clap is None:
            _clap = (ClapProcessor.from_pretrained(CLAP_ID), ClapModel.from_pretrained(CLAP_ID).eval())
        import torch

        want = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
        _clap[1].to(want)
        return _clap[0], _clap[1], want


def release_clap() -> None:
    global _clap
    with _lock:
        _clap = None
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def clap_similarity(path: str | Path, text: str, device: str = "cpu", n_windows: int = 4) -> float:
    """Косинусная близость аудио (n окон по 10 с, равномерно по треку) и английского описания в пространстве CLAP.

    Абсолютные значения малы (~0.1–0.4); осмысленно только сравнение вариантов с одним и тем же описанием.
    """
    import torch
    import torchaudio

    wav, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = torchaudio.functional.resample(torch.from_numpy(wav.mean(1)), sr, 48000).numpy()
    win = 10 * 48000
    starts = np.linspace(0, max(len(mono) - win, 0), n_windows).astype(int)
    pieces = [mono[s:s + win] for s in starts]
    proc, model, dev = _get_clap(device)
    inputs = proc(text=[text], audios=pieces, sampling_rate=48000, return_tensors="pt", padding=True)
    inputs = {k: v.to(dev) for k, v in inputs.items()}
    with torch.inference_mode():
        out = model(**inputs)
    return float((out.audio_embeds @ out.text_embeds.T).mean().item())


# ---------------------------------------------------------------- разборчивость вокала (Whisper)
WHISPER_ID = "openai/whisper-large-v3-turbo"
_whisper = None
_whisper_device = None


def _get_whisper(device: str):
    global _whisper, _whisper_device
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor

    want = "cuda" if device == "cuda" and torch.cuda.is_available() else "cpu"
    with _lock:
        if _whisper is None:
            proc = WhisperProcessor.from_pretrained(WHISPER_ID)
            model = WhisperForConditionalGeneration.from_pretrained(WHISPER_ID, dtype=torch.float32).eval()
            _whisper = (proc, model)
        if _whisper_device != want:
            _whisper[1].to(want).to(torch.float16 if want == "cuda" else torch.float32)
            _whisper_device = want
        return _whisper[0], _whisper[1], want


def release_whisper() -> None:
    global _whisper, _whisper_device
    with _lock:
        _whisper, _whisper_device = None, None
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def transcribe(path: str | Path, device: str = "cuda", language: str = "ru", max_seconds: float = 150.0) -> str:
    """Расшифровывает вокал (окнами по 30 с, не более max_seconds — дальше обычно повтор припева)."""
    import torch
    import torchaudio

    wav, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = torchaudio.functional.resample(torch.from_numpy(wav.mean(1)), sr, 16000).numpy()
    mono = mono[: int(max_seconds * 16000)]
    proc, model, dev = _get_whisper(device)
    chunk = 30 * 16000
    pieces = [mono[i:i + chunk] for i in range(0, len(mono), chunk) if len(mono) - i > 16000]
    texts: list[str] = []
    for i in range(0, len(pieces), 4):
        feats = proc(pieces[i:i + 4], sampling_rate=16000, return_tensors="pt")
        feats = {k: v.to(dev, dtype=model.dtype) if v.is_floating_point() else v.to(dev) for k, v in feats.items()}
        with torch.inference_mode():
            ids = model.generate(**feats, language=language, task="transcribe")
        texts += [t.strip() for t in proc.batch_decode(ids, skip_special_tokens=True)]
    return " ".join(texts)


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-zа-яё ]", " ", text.lower().replace("ё", "е")).replace("[", " ").split()


def lyrics_match(transcript: str, lyrics: str) -> dict[str, float]:
    """Насколько спетое совпадает с текстом: precision (спетые слова есть в тексте) и coverage (слова текста прозвучали).

    Теги структуры [Verse]… из текста исключаются. Сравнение нечёткое (в вокале слова искажаются на 1–2 буквы).
    """
    ref_words = _words(re.sub(r"\[[^\]]*\]", " ", lyrics))
    hyp = _words(transcript)
    vocab = sorted(set(ref_words))
    if not vocab or not hyp:
        return {"precision": 0.0, "coverage": 0.0, "lyrics_score": 0.0}

    def close(w: str, pool: list[str]) -> bool:
        return w in pool or bool(difflib.get_close_matches(w, pool, n=1, cutoff=0.78))

    precision = float(np.mean([close(w, vocab) for w in hyp]))
    hyp_set = sorted(set(hyp))
    coverage = float(np.mean([close(w, hyp_set) for w in vocab]))
    return {"precision": precision, "coverage": coverage, "lyrics_score": 0.6 * precision + 0.4 * coverage}


CLAP_WEIGHT = 5.0      # +0.1 близости к описанию ≈ +0.5 балла (на практике разброс CLAP между вариантами ~0.05–0.15)
LYRICS_WEIGHT = 4.0    # +0.2 разборчивости текста ≈ +0.8 балла


def total_score(aes: dict[str, Any], lyr: dict[str, float] | None, clap: float | None = None) -> float:
    """Итоговый балл кандидата: эстетика + (для вокала) разборчивость текста + соответствие описанию (CLAP)."""
    t = overall(aes)
    if lyr is not None:
        t += LYRICS_WEIGHT * lyr["lyrics_score"]
    if clap is not None:
        t += CLAP_WEIGHT * clap
    return float(t)


# ---------------------------------------------------------------- узнаваемость оригинала в кавере (хрома)
RETENTION_WEIGHT = 2.0   # +0.1 похожести гармонии ≈ +0.2 балла
RETENTION_CAP = 0.30     # выше — уже «копия», а не кавер: дальше не поощряем


def _chroma(path: str | Path, n_fft: int = 8192, hop: int = 4096) -> np.ndarray:
    """Хромаграмма 12×кадры по STFT: энергия 55–2000 Гц, свёрнутая в классы высот; нормирована и центрирована."""
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    y = y.mean(1)
    if len(y) < n_fft:
        return np.zeros((12, 0), dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop] * np.hanning(n_fft).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = (freqs >= 55) & (freqs <= 2000)
    pc = np.round(12 * np.log2(freqs[band] / 440.0)).astype(int) % 12
    sb = spec[:, band]
    chroma = np.stack([sb[:, pc == k].sum(1) for k in range(12)]).astype(np.float32)
    chroma = np.sqrt(chroma)
    chroma /= np.linalg.norm(chroma, axis=0) + 1e-8
    return chroma - chroma.mean(0)


def chroma_similarity(a: str | Path, b: str | Path) -> float:
    """Похожесть гармонии по времени 1:1 (кавер той же длины): ~0 — не связаны, 0.2–0.3 — узнаваемо, >0.7 — почти копия."""
    ca, cb = _chroma(a), _chroma(b)
    n = min(ca.shape[1], cb.shape[1])
    if n == 0:
        return 0.0
    ca, cb = ca[:, :n], cb[:, :n]
    cos = (ca * cb).sum(0) / (np.linalg.norm(ca, axis=0) * np.linalg.norm(cb, axis=0) + 1e-8)
    return float(cos.mean())


def cover_score(aes: dict[str, Any], clap: float, retention: float, lyr: dict[str, float] | None = None) -> float:
    """Балл кавера: эстетика + новый стиль (CLAP) + узнаваемость оригинала (до потолка) + разборчивость текста.

    Без узнаваемости ранжирование выбирало «чистые» варианты, где от мелодии исходника ничего не осталось,
    а без потолка — почти копии исходника без смены стиля (замеры: scripts/cover_lab.py, data/lab/cover_results.jsonl).
    """
    return total_score(aes, lyr, clap) + RETENTION_WEIGHT * min(retention, RETENTION_CAP)
