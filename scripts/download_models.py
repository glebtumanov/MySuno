"""Докачка дополнительных моделей ACE-Step: python scripts/download_models.py sft xl-turbo xl-sft lm-4B [lm-0.6B base]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mysuno import config  # noqa: E402

ALIASES = {
    "sft": "acestep-v15-sft", "base": "acestep-v15-base", "turbo": "acestep-v15-turbo",
    "xl-turbo": "acestep-v15-xl-turbo", "xl-sft": "acestep-v15-xl-sft", "xl-base": "acestep-v15-xl-base",
    "lm-0.6B": "acestep-5Hz-lm-0.6B", "lm-1.7B": "acestep-5Hz-lm-1.7B", "lm-4B": "acestep-5Hz-lm-4B",
}


def main() -> int:
    from acestep.model_downloader import ensure_dit_model, ensure_lm_model

    rc = 0
    for arg in sys.argv[1:]:
        name = ALIASES.get(arg, arg)
        fn = ensure_lm_model if "-lm-" in name else ensure_dit_model
        print(f"== {name}", flush=True)
        ok, msg = fn(name, config.CHECKPOINTS_DIR)
        print(("OK: " if ok else "FAIL: ") + msg, flush=True)
        rc |= 0 if ok else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
