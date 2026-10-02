"""Объективные метрики «шумности» аудио: python scripts/audio_quality.py файл.flac [...] [--window 15]

Для каждого окна считает:
  flat  — спектральная флэтность 100 Гц–16 кГц (белый шум ≈ 0.5–1, музыка ≈ 0.001–0.05);
  hf    — доля энергии выше 8 кГц;
  rms   — громкость, дБFS;
  rep   — «повторяемость»: средняя корреляция хромаграммы с соседними окнами (тональная связность, 0..1).
Окна с flat > 0.15 помечаются как шумовые. Это грубый детектор «каши», он не заменяет прослушивание.
"""
import argparse
import sys

import numpy as np
import soundfile as sf

SR_MIN, SR_MAX = 100, 16000


def frames_flatness(x: np.ndarray, sr: int, n_fft: int = 2048, hop: int = 1024):
    win = np.hanning(n_fft).astype(np.float32)
    n = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2 + 1e-12
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = (freqs >= SR_MIN) & (freqs <= SR_MAX)
    s = spec[:, band]
    flat = np.exp(np.log(s).mean(1)) / s.mean(1)
    hf = spec[:, freqs > 8000].sum(1) / spec.sum(1)
    chroma = np.zeros((n, 12), dtype=np.float32)
    pcs = (np.round(12 * np.log2(np.maximum(freqs[band], 1) / 440.0)) % 12).astype(int)
    for k in range(12):
        chroma[:, k] = s[:, pcs == k].sum(1)
    chroma /= chroma.sum(1, keepdims=True) + 1e-9
    return flat, hf, chroma


def analyze(path: str, window_s: float):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = x.mean(1)
    hop = 1024
    flat, hf, chroma = frames_flatness(mono, sr, hop=hop)
    per = int(window_s * sr / hop)
    rows = []
    for i in range(0, len(flat), per):
        sl = slice(i, i + per)
        seg = mono[i * hop:(i + per) * hop]
        c = chroma[sl].mean(0)
        rows.append({"t": i * hop / sr, "flat": float(np.median(flat[sl])), "hf": float(hf[sl].mean()),
                     "rms": 20 * np.log10(np.sqrt((seg ** 2).mean()) + 1e-9), "chroma": c})
    for a, b in zip(rows, rows[1:] + [rows[-1]]):
        a["rep"] = float(np.corrcoef(a["chroma"], b["chroma"])[0, 1])
    return rows, len(mono) / sr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--window", type=float, default=15.0)
    ap.add_argument("--quiet", action="store_true", help="только итоговая строка по файлу")
    args = ap.parse_args()
    for p in args.files:
        rows, dur = analyze(p, args.window)
        noisy = [r for r in rows if r["flat"] > 0.15]
        flats = np.array([r["flat"] for r in rows])
        print(f"{p.split(chr(92))[-1][:28]:28} {dur:5.0f}s  flat медиана {np.median(flats):.4f}  макс {flats.max():.3f}  "
              f"шумовых окон {len(noisy)}/{len(rows)}  hf {np.mean([r['hf'] for r in rows]):.3f}  rep {np.mean([r['rep'] for r in rows]):.2f}")
        if not args.quiet:
            line = "   flat по окнам: " + " ".join(f"{r['flat']:.3f}" for r in rows)
            print(line)
            print("   t (с):        " + " ".join(f"{r['t']:5.0f}" for r in rows))


if __name__ == "__main__":
    sys.exit(main())
