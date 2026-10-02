"""Скачивание весов: python -m mysuno.download  (ACE-Step + переводчик ru→en)."""
from __future__ import annotations

import sys

from . import config


def main() -> int:
    from acestep.model_downloader import ensure_main_model

    print(f"Каталог чекпоинтов: {config.CHECKPOINTS_DIR}")
    ok, msg = ensure_main_model(config.CHECKPOINTS_DIR)
    print(msg)
    if not ok:
        return 1

    print("Загрузка переводчика ru→en (Helsinki-NLP/opus-mt-ru-en, ~300 МБ)…")
    try:
        from transformers import MarianMTModel, MarianTokenizer

        from .translate import MODEL_ID

        MarianTokenizer.from_pretrained(MODEL_ID)
        MarianMTModel.from_pretrained(MODEL_ID)
    except Exception as exc:  # noqa: BLE001
        print(f"Переводчик не скачался ({exc}); приложение будет работать без перевода.")
    print("Загрузка оценщиков качества (Audiobox ~0.4 ГБ, CLAP ~1.1 ГБ, Whisper ~1.5 ГБ) — нужны для режимов «Хорошо/Максимум»…")
    try:
        from huggingface_hub import snapshot_download

        from . import quality

        for repo in ("facebook/audiobox-aesthetics", quality.CLAP_ID, quality.WHISPER_ID):
            snapshot_download(repo, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.bin"]
                              if repo != quality.WHISPER_ID else ["*.json", "*.safetensors", "*.txt"])
    except Exception as exc:  # noqa: BLE001
        print(f"Оценщики не скачались ({exc}); они загрузятся при первом использовании режима выбора лучшего.")
    print("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
