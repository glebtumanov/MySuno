"""Smoke-тест без UI: python scripts/smoke_test.py [--cpu] [--seconds 15] [--vocal]

Загружает модели, генерирует короткий трек и печатает время. Результат — в data/tmp/smoke.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mysuno import config  # noqa: E402
from mysuno.engine import get_engine, hardware  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", action="store_true", help="принудительно CPU, без LM")
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--vocal", action="store_true", help="с вокалом (русский текст)")
    ap.add_argument("--no-lm", action="store_true")
    args = ap.parse_args()

    print("Железо:", hardware())
    settings = config.load_settings()
    if args.cpu:
        settings.update(device="cpu", lm_model="none")
    if args.no_lm:
        settings["lm_model"] = "none"

    def progress(value, desc):
        print(f"  [{value * 100:5.1f}%] {desc}")

    req = {
        "prompt": "спокойная лирическая композиция, осенний дождь",
        "genres": ["ambient"] if not args.vocal else ["pop", "ballad"],
        "vocal": "female" if args.vocal else "none",
        "lyrics": "[verse]\nДождь стучит по крыше,\nТихо город спит.\n\n[chorus]\nЯ тебя услышу,\nСердце говорит." if args.vocal else "",
        "duration": args.seconds,
        "temperature": 0.85,
        "batch": 1,
        "format": "flac",
    }
    eng = get_engine()
    t0 = time.time()
    out = eng.generate(req, settings, progress, config.TMP_DIR / "smoke")
    print(f"\nГотово за {time.time() - t0:.1f} с (генерация {out['gen_seconds']} с)")
    print("План:", out["plan"])
    print("Caption:", out["caption"])
    for t in out["tracks"]:
        print("Файл:", t["path"], f"{t['duration']} с, seed={t['seed']}")
    print("Тайминги:", out["time_costs"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
