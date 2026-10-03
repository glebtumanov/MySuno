"""Лаборатория каверов: варианты параметров на одном исходнике в одном процессе (сервер должен быть остановлен).

python scripts/cover_lab.py run <аудио> --start 30 --end 90 --caption "synthwave, retro 80s synths" --seeds 1,2 \
    --variants '[{"name":"s0.6","cover_strength":0.6}, {"name":"n0.2","cover_strength":0,"cover_noise":0.2}]'

Для каждого трека: Audiobox CE/PQ (низкий CE = «какофония»), CLAP к целевому описанию (смена стиля),
совпадение семантических 5-Гц кодов ACE с исходником и хрома (сохранение мелодии/структуры).
Результаты дописываются в data/lab/cover_results.jsonl, сводка печатается в конце.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from mysuno import config, quality, sources  # noqa: E402
from mysuno.engine import get_engine  # noqa: E402
from cover_check import chroma_similarity as chroma_sim  # noqa: E402

OUT = config.DATA / "lab" / "cover_results.jsonl"
_CODE = re.compile(r"<\|audio_code_(\d+)\|>")


def codes_of(engine, path: str) -> list[int]:
    return [int(x) for x in _CODE.findall(engine.dit.convert_src_audio_to_codes(path) or "")]


def code_match(a: list[int], b: list[int]) -> float:
    n = min(len(a), len(b))
    return sum(x == y for x, y in zip(a[:n], b[:n])) / n if n else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("audio")
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--end", type=float, default=60)
    ap.add_argument("--caption", default="synthwave, retro 80s synths")
    ap.add_argument("--lyrics", default="")
    ap.add_argument("--seeds", default="1,2")
    ap.add_argument("--variants", required=True, help="JSON-список: name + поля запроса кавера")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    settings = config.load_settings()
    engine = get_engine()
    engine.ensure_loaded(settings, lambda v, d: None)
    tmp = config.TMP_DIR / "cover_lab"
    src, length = sources.cut_segment(Path(args.audio), args.start, args.end, tmp)
    src_copy = tmp / "src_ref.wav"
    if src != src_copy:
        import shutil

        shutil.copy(src, src_copy)
    src_codes = codes_of(engine, str(src_copy))
    print(f"source {length:.1f} s, {len(src_codes)} codes; plan {engine.plan}")
    from acestep.inference import understand_music

    t = time.time()
    u = understand_music(engine.llm, "".join(f"<|audio_code_{c}|>" for c in src_codes), use_constrained_decoding=True)
    und = {"caption": u.caption, "bpm": u.bpm, "keyscale": u.keyscale, "timesignature": u.timesignature,
           "lyrics": u.lyrics, "language": getattr(u, "language", "")}
    print(f"понимание исходника ({time.time() - t:.1f} с): {json.dumps(und, ensure_ascii=False)[:600]}")

    variants = json.loads(args.variants)
    rows = []
    for v in variants:
        for seed in [int(s) for s in args.seeds.split(",")]:
            req = {"task": "cover", "_src_path": str(src_copy), "duration": length, "prompt": args.caption,
                   "lyrics": args.lyrics, "vocal": "auto", "seed": seed, "guard": False, "batch": 1,
                   "cover_strength": 0.6, "cover_noise": 0.0, "cover_mode": "cover",
                   **{k: x for k, x in v.items() if k not in ("name", "metas")}}
            if v.get("metas"):   # BPM/тональность/размер исходника из «понимания» LM
                req.update(bpm=und["bpm"], keyscale=und["keyscale"], timesignature=und["timesignature"])
            if req.get("source_lyrics") == "auto":
                req["source_lyrics"] = und["lyrics"] or "[Instrumental]"
            if req.get("source_caption") == "auto":
                req["source_caption"] = und["caption"]
            out = tmp / f"{v['name']}_{seed}"
            t = time.time()
            res = engine.generate(req, {**settings, "translate": False, "audio_format": "flac"}, lambda *_: None, out)
            path = res["tracks"][0]["path"]
            rows.append({"name": v["name"], "seed": seed, "path": path, "secs": round(time.time() - t, 1),
                         "caption": res["caption"], "req": {k: x for k, x in req.items() if not k.startswith("_")}})
            print(f"  {v['name']} seed {seed}: {rows[-1]['secs']} s")
    # оценки: коды (DiT ещё в VRAM), затем оценщики с освобождением памяти
    for r in rows:
        r["code_match"] = round(code_match(src_codes, codes_of(engine, r["path"])), 3)
        r["chroma"] = round(chroma_sim(str(src_copy), r["path"]), 3)
    device = engine._scorer_device()
    with engine._scoring_headroom():
        device = engine._scorer_device()
        for r in rows:
            aes = quality.score_file(r["path"], device=device)
            r.update(CE=round(float(aes["CE"]), 3), PQ=round(float(aes["PQ"]), 3),
                     clap=round(quality.clap_similarity(r["path"], args.caption, device=device), 3))
        src_aes = quality.score_file(str(src_copy), device=device)
        src_clap = quality.clap_similarity(str(src_copy), args.caption, device=device)
    quality.release(); quality.release_clap()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({**r, "tag": args.tag, "source": str(args.audio), "start": args.start,
                                "end": args.end, "plan": engine.plan}, ensure_ascii=False) + "\n")
    print(f"\nисходник: CE {float(src_aes['CE']):.2f} PQ {float(src_aes['PQ']):.2f} CLAP→стиль {src_clap:.3f}")
    print(f"{'вариант':<22}{'CE':>6}{'PQ':>6}{'CLAP':>7}{'коды':>7}{'хрома':>7}")
    for name in dict.fromkeys(r["name"] for r in rows):
        rs = [r for r in rows if r["name"] == name]
        avg = lambda k: sum(r[k] for r in rs) / len(rs)  # noqa: E731
        print(f"{name:<22}{avg('CE'):>6.2f}{avg('PQ'):>6.2f}{avg('clap'):>7.3f}{avg('code_match'):>7.3f}{avg('chroma'):>7.3f}")


if __name__ == "__main__":
    main()
