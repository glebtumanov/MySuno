"""Профилирование фаз генерации: python scripts/profile_lm.py [--seconds 120] [--runs 3]

Грузит движок в этом процессе (сервер должен быть остановлен), делает прогрев и несколько прогонов
с LM-планированием; печатает тайминги фаз (LM phase1/phase2, DiT, VAE) и токены/с LM.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mysuno import config  # noqa: E402
from mysuno.engine import get_engine  # noqa: E402

LYRICS = ("[verse]\nДождь стучит по крыше,\nТихо город спит.\nЯ тебя услышу,\nСердце говорит.\n\n"
          "[chorus]\nОсень, осень, осень,\nТы со мной одна.\nПусть идут дожди,\nНе нужна весна.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--lm", default=None, help="auto | none | acestep-5Hz-lm-0.6B | acestep-5Hz-lm-1.7B")
    ap.add_argument("--no-lm-thinking", action="store_true")
    args = ap.parse_args()

    settings = config.load_settings()
    if args.lm:
        settings["lm_model"] = args.lm
    eng = get_engine()
    req = {"prompt": "лирическая поп-баллада", "genres": ["pop", "ballad"], "vocal": "female",
           "lyrics": LYRICS, "duration": args.seconds, "batch": 1, "seed": 7,
           "thinking": not args.no_lm_thinking, "format": "flac"}

    t0 = time.time()
    eng.ensure_loaded(settings, lambda v, d: None)
    print(f"загрузка моделей: {time.time() - t0:.1f} с, план: {eng.plan}")

    def one(label: str) -> float:
        t = time.time()
        out = eng.generate(req, settings, lambda v, d: None, config.TMP_DIR / "profile")
        dt = time.time() - t
        tc = out["time_costs"]
        p1, p2 = tc.get("lm_phase1_time", 0), tc.get("lm_phase2_time", 0)
        tokens = args.seconds * 5
        print(f"{label}: всего {dt:5.1f} с | LM ф1 {p1:5.1f} | LM ф2 {p2:5.1f} ({tokens / max(p2, 1e-6):5.1f} ток/с) | "
              f"DiT {tc.get('dit_total_time_cost', 0):5.1f} | pipeline {tc.get('pipeline_total_time', 0):5.1f}")
        return dt

    one("прогрев ")
    times = [one(f"прогон {i + 1}") for i in range(args.runs)]
    print(f"\nсреднее: {sum(times) / len(times):.1f} с на {args.seconds} с аудио")
    out = eng.generate(req, settings, lambda v, d: None, config.TMP_DIR / "profile")
    print("\nвсе тайминги ACE (с):")
    for k, v in sorted(out["time_costs"].items()):
        if isinstance(v, (int, float)):
            print(f"  {k:40} {v:7.3f}")
    print("этапы движка (с):", out.get("stages"))


if __name__ == "__main__":
    main()
