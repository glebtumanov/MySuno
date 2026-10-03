"""Пути проекта и сохраняемые настройки."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TRACKS_DIR = DATA / "tracks"
TMP_DIR = DATA / "tmp"
SOURCES_DIR = DATA / "sources"   # загруженные исходники для каверов
DB_FILE = DATA / "library.db"
SETTINGS_FILE = DATA / "settings.json"
UI_STATE_FILE = DATA / "ui_state.json"   # содержимое форм «Создать» и «Каверы» (переживает перезапуск сервера)
ACE_ROOT = ROOT / "vendor" / "ACE-Step-1.5"
CHECKPOINTS_DIR = ACE_ROOT / "checkpoints"
WEB_DIR = Path(__file__).resolve().parent / "web"

# Кэш Hugging Face держим внутри проекта (переводчик ru→en и т.п.).
os.environ.setdefault("HF_HOME", str(DATA / "hf"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

if ACE_ROOT.exists() and str(ACE_ROOT) not in sys.path:
    sys.path.insert(0, str(ACE_ROOT))

for _d in (DATA, TRACKS_DIR, TMP_DIR, SOURCES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DIT_MODELS = ("acestep-v15-turbo", "acestep-v15-sft", "acestep-v15-base")
LM_MODELS = ("auto", "none", "acestep-5Hz-lm-0.6B", "acestep-5Hz-lm-1.7B")

DEFAULT_SETTINGS: dict[str, Any] = {
    "device": "auto",                  # auto | cuda | cpu
    "dit_model": "acestep-v15-turbo",
    "lm_model": "auto",                # auto | none | acestep-5Hz-lm-0.6B | acestep-5Hz-lm-1.7B
    "lm_backend": "auto",              # auto | vllm | pt
    "offload": "auto",                 # auto | on | off
    "quantization": "auto",            # auto | int8 | off
    "audio_format": "flac",            # flac | mp3 | wav
    "max_duration": 300,               # секунд, верхняя граница ползунка
    "translate": True,                 # переводить русское описание на английский
    "fast_lm": True,                   # ускоренный LM-декодер на CUDA Graphs (только CUDA, bf16)
    "preload": True,                   # загружать и прогревать модели при запуске сервера
}

_ALLOWED: dict[str, tuple] = {
    "device": ("auto", "cuda", "cpu"),
    "dit_model": DIT_MODELS,
    "lm_model": LM_MODELS,
    "lm_backend": ("auto", "vllm", "pt"),
    "offload": ("auto", "on", "off"),
    "quantization": ("auto", "int8", "off"),
    "audio_format": ("flac", "mp3", "wav"),
}

_lock = threading.Lock()


def load_settings() -> dict[str, Any]:
    """Читает settings.json, дополняя значениями по умолчанию."""
    data: dict[str, Any] = {}
    with _lock:
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    return merged


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Валидирует и сохраняет изменения настроек, возвращает полный набор."""
    current = load_settings()
    for key, value in patch.items():
        if key not in DEFAULT_SETTINGS:
            continue
        if key in _ALLOWED:
            if value not in _ALLOWED[key]:
                raise ValueError(f"Недопустимое значение {value!r} для {key}")
        elif key == "max_duration":
            value = max(10, min(600, int(value)))
        elif key in ("translate", "fast_lm", "preload"):
            value = bool(value)
        current[key] = value
    with _lock:
        SETTINGS_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current
