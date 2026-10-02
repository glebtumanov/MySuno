"""Оценка качества треков из библиотеки: python scripts/score_tracks.py [id|путь ...]  (без аргументов — вся библиотека)"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mysuno import config, quality  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    items: list[tuple[str, Path]] = []
    if args and Path(args[0]).exists():
        items = [(Path(a).name, Path(a)) for a in args]
    else:
        db = sqlite3.connect(config.DB_FILE)
        rows = db.execute("SELECT id, title, filename, duration FROM tracks ORDER BY created_at").fetchall()
        for tid, title, fn, dur in rows:
            if not args or tid in args:
                items.append((f"{tid[:8]} {title[:34]} {dur:.0f}s", config.TRACKS_DIR / fn))
    print(f"{'трек':50} {'PQ':>5} {'CE':>5} {'CEp10':>6} {'CEmin':>6} {'flat':>6} {'шум%':>5} {'стат%':>5} {'итог':>6}")
    for name, path in items:
        s = quality.score_file(path)
        print(f"{name:50} {s['PQ']:5.2f} {s['CE']:5.2f} {s['CE_p10']:6.2f} {s['CE_min']:6.2f} "
              f"{s['flat_median']:6.3f} {s['noise_ratio'] * 100:4.0f}% {s['static'] * 100:4.0f}% {quality.overall(s):6.2f}", flush=True)


if __name__ == "__main__":
    main()
