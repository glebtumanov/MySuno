"""Проверка каверов через HTTP API запущенного сервера: несколько значений силы на одном исходнике.

python scripts/cover_check.py <аудиофайл> [--genres jazz] [--prompt "..."] [--strengths 0.3,0.5,0.8]
Печатает id треков; сходство с исходником (хрома) и с описанием (CLAP) считает --score.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = "http://127.0.0.1:8000/api"


def call(path: str, body=None, method: str | None = None, raw: bytes | None = None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers=headers or ({"Content-Type": "application/json"} if body is not None else {}))
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _chroma(path: Path, n_fft: int = 8192, hop: int = 4096):
    """Хромаграмма 12×кадры по STFT (без librosa): энергия бинов 55–2000 Гц, свёрнутая в классы высот."""
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    y = y.mean(1)
    frames = np.lib.stride_tricks.sliding_window_view(y, n_fft)[::hop] * np.hanning(n_fft)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    band = (freqs >= 55) & (freqs <= 2000)
    pc = np.round(12 * np.log2(freqs[band] / 440.0)).astype(int) % 12
    chroma = np.zeros((12, spec.shape[0]), dtype=np.float32)
    for k in range(12):
        chroma[k] = spec[:, band][:, pc == k].sum(1)
    return np.sqrt(chroma)


def chroma_similarity(a: Path, b: Path) -> float:
    """Похожесть гармонии по кадрам (кавер той же длины, время 1:1): косинус центрированных нормированных хромаграмм.

    Центрирование убирает общий «шумовой пол», так что ~0 = не связаны, выше = та же гармония в те же моменты.
    """
    import numpy as np

    def prep(path):
        c = _chroma(path)
        c = c / (np.linalg.norm(c, axis=0) + 1e-8)
        return c - c.mean(0)

    ca, cb = prep(a), prep(b)
    n = min(ca.shape[1], cb.shape[1])
    ca, cb = ca[:, :n], cb[:, :n]
    cos = (ca * cb).sum(0) / (np.linalg.norm(ca, axis=0) * np.linalg.norm(cb, axis=0) + 1e-8)
    return float(cos.mean())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--genres", default="jazz")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--strengths", default="0.3,0.5,0.8,1.0")
    ap.add_argument("--end", type=float, default=60)
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()

    src = Path(args.audio)
    meta = call("/sources", raw=src.read_bytes(),
                headers={"X-Filename": urllib.parse.quote(src.name)})
    print("source", meta["id"], meta["duration"])
    jobs = {}
    for s in [float(x) for x in args.strengths.split(",")]:
        job = call("/cover", {"source_id": meta["id"], "source_name": meta["name"], "end": args.end,
                              "genres": args.genres.split(",") if args.genres else [], "prompt": args.prompt,
                              "cover_strength": s, "batch": 1, "guard": False, "seed": 42})
        jobs[job["id"]] = s
    results = {}
    while len(results) < len(jobs):
        time.sleep(2)
        for j in call("/jobs")["jobs"]:
            if j["id"] in jobs and j["status"] in ("done", "error", "cancelled") and j["id"] not in results:
                results[j["id"]] = j
                print(f"strength {jobs[j['id']]}: {j['status']} {j.get('error') or ''} tracks={j['track_ids']} "
                      f"{j['elapsed']} s")
    if not args.score:
        return
    from mysuno import config, quality
    from mysuno.db import get_library

    lib = get_library()
    ref = config.TMP_DIR / "cover_ref.wav"
    import soundfile as sf

    data, sr = sf.read(str(src), stop=int(args.end * sf.info(str(src)).samplerate))
    sf.write(str(ref), data, sr)
    caption = None
    for job_id, j in results.items():
        for tid in j["track_ids"]:
            t = lib.get_track(tid)
            path = config.TRACKS_DIR / t["filename"]
            caption = t["caption"]
            print(f"strength {jobs[job_id]}: chroma→source {chroma_similarity(ref, path):.3f} "
                  f"CLAP→caption {quality.clap_similarity(str(path), caption):.3f}")
    print(f"source itself: CLAP→caption {quality.clap_similarity(str(ref), caption):.3f}")


if __name__ == "__main__":
    main()
