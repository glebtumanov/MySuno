"""Работают ли негативные жанры: сравнение «рок без металла/панка» и «просто рок» по CLAP-близости к тяжёлым описаниям.

python scripts/neg_eval.py pos_cfg2 neg_cfg2 pos_cfg5 neg_cfg5     (файлы из lab/results.jsonl, промпт neg_rock)
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mysuno import config, quality  # noqa: E402

LAB = config.DATA / "lab"
HEAVY = "heavy metal music with aggressive distorted guitars, screaming and double bass drums"
HARD = "loud punk rock with raw fast distorted guitars"
SOFT = "mellow melodic rock with clean guitars and soft drums"


def main() -> None:
    exps = sys.argv[1:]
    recs = [json.loads(x) for x in (LAB / "results.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"{'конфигурация':12} {'n':>3} {'тяжёлый':>8} {'панк':>7} {'мягкий':>7}   (CLAP-близость; меньше «тяжёлого» = негатив работает)")
    for exp in exps:
        rows = [r for r in recs if r["exp"] == exp and r["prompt"] == "neg_rock" and Path(r["path"]).exists()]
        if not rows:
            continue
        vals = np.array([[quality.clap_similarity(r["path"], t) for t in (HEAVY, HARD, SOFT)] for r in rows])
        print(f"{exp:12} {len(rows):3} {vals[:, 0].mean():8.3f} {vals[:, 1].mean():7.3f} {vals[:, 2].mean():7.3f}")


if __name__ == "__main__":
    main()
