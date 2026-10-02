"""Перевод русских описаний в английские теги на CPU (Helsinki-NLP/opus-mt-ru-en).

Модель небольшая (~300 МБ) и работает на процессоре, не занимая GPU. Если модель
недоступна (нет сети/пакета), текст передаётся как есть.
"""
from __future__ import annotations

import contextlib
import re
import threading

from loguru import logger

MODEL_ID = "Helsinki-NLP/opus-mt-ru-en"
_CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

_lock = threading.Lock()
_tokenizer = None
_model = None
_failed = False


@contextlib.contextmanager
def _float32_default():
    """Временно возвращает глобальный default dtype в float32 (HF создаёт служебные тензоры в default dtype)."""
    import torch

    prev = torch.get_default_dtype()
    torch.set_default_dtype(torch.float32)
    try:
        yield
    finally:
        torch.set_default_dtype(prev)


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC.search(text or ""))


def _load() -> bool:
    global _tokenizer, _model, _failed
    if _model is not None:
        return True
    if _failed:
        return False
    try:
        import torch
        from transformers import MarianMTModel, MarianTokenizer

        _tokenizer = MarianTokenizer.from_pretrained(MODEL_ID)
        # ACE-Step на время загрузки DiT переключает глобальный default dtype на bfloat16 — переводчик всегда fp32
        with _float32_default():
            _model = MarianMTModel.from_pretrained(MODEL_ID, dtype=torch.float32).to("cpu").eval()
        logger.info("Переводчик ru→en загружен (CPU, потоков torch: {})", torch.get_num_threads())
        return True
    except Exception as exc:  # noqa: BLE001 — любой сбой = работаем без перевода
        _failed = True
        logger.warning("Переводчик недоступен, русский текст передаётся как есть: {}", exc)
        return False


def warmup() -> None:
    """Загружает модель и делает пробный перевод (вызывать в фоне при старте, чтобы первый запрос не ждал ~2.5 с)."""
    ru_to_en("тест")


def ru_to_en(text: str) -> str:
    """Переводит текст, если в нём есть кириллица; иначе возвращает без изменений."""
    text = (text or "").strip()
    if not text or not has_cyrillic(text):
        return text
    with _lock:
        if not _load():
            return text
        import torch

        batch = _tokenizer([text], return_tensors="pt", truncation=True, max_length=256)
        with torch.inference_mode(), _float32_default():
            out = _model.generate(**batch, num_beams=4, max_new_tokens=128)
        return _tokenizer.batch_decode(out, skip_special_tokens=True)[0].strip()
