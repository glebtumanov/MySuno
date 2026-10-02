"""LLMHandler ACE-Step с быстрым декодером на CUDA Graphs (см. fastlm.py).

Подменяется только прямой проход `_forward_pass`: вся логика ACE (CFG, FSM ограничённого декодирования,
сэмплирование, фазы CoT/коды) остаётся оригинальной. Если быстрый путь недоступен (CPU, offload, нестандартная
модель, не хватило ёмкости кэша) — прозрачный откат на штатный HF-проход.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from loguru import logger

from acestep.constrained_logits_processor import MAX_AUDIO_CODE, FSMState
from acestep.llm_inference import LLMHandler

from .fastlm import FastQwen3

DEFAULT_MAX_NEW = 2048


class FastLLMHandler(LLMHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fast_enabled = True
        self.fast: FastQwen3 | None = None
        self._max_new_hint: int | None = None
        self._cp = None   # constrained_processor текущей генерации (для определения фазы кодов)
        self.cancel_check: Callable[[], None] | None = None   # бросает исключение, если генерацию отменили
        self.codes_rep_penalty = 1.0                          # >1.0 — штраф за повторы на фазе кодов

    # ---- загрузка / выгрузка ----
    def _load_pytorch_model(self, model_path: str, device: str):
        ok, msg = super()._load_pytorch_model(model_path, device)
        if ok:
            self._enable_fast(device)
        return ok, msg

    def _enable_fast(self, device: str) -> None:
        self.fast = None
        if not self.fast_enabled or device != "cuda" or self.offload_to_cpu:
            return
        try:
            fast = FastQwen3(self.llm)
            code_range = self._audio_code_range()
            if code_range:
                eos = self.llm_tokenizer.eos_token_id    # ACE разрешает EOS в фазе кодов (контроль длительности)
                fast.set_code_range(*code_range, extra=[] if eos is None else [eos])
            secs = fast.warmup()
            self.fast = fast
            logger.info("Быстрый LM-декодер (CUDA Graphs) включён, прогрев {:.1f} с, режим кодов: {}", secs,
                        "да" if code_range else "нет")
        except Exception as exc:  # noqa: BLE001 — любой сбой = штатный путь
            logger.warning("Быстрый LM-декодер недоступен, работаю штатно: {}", exc)
            self.fast = None

    def _audio_code_range(self) -> tuple[int, int] | None:
        """[lo, hi) id допустимых токенов аудио-кодов (0..MAX_AUDIO_CODE), если они идут подряд."""
        tok = self.llm_tokenizer
        if tok is None:
            return None
        lo = tok.convert_tokens_to_ids("<|audio_code_0|>")
        hi = tok.convert_tokens_to_ids(f"<|audio_code_{MAX_AUDIO_CODE}|>")
        unk = getattr(tok, "unk_token_id", None)
        if lo is None or hi is None or lo == unk or hi == unk or hi - lo != MAX_AUDIO_CODE:
            return None
        return lo, hi + 1

    def _in_codes_phase(self) -> bool:
        cp = self._cp
        return cp is not None and getattr(cp, "state", None) == FSMState.CODES_GENERATION

    def unload(self) -> None:
        if self.fast is not None:
            self.fast.free()
            self.fast = None
        super().unload()

    # ---- штраф за повторы только на фазе аудио-кодов ----
    def _build_logits_processor(self, repetition_penalty: float):
        """ACE не даёт штрафа за повторы для кодов; добавляем опционально (против зацикливания длинных треков).
        Только фаза кодов: на текстовой фазе (метаданные/caption) штраф портит вывод."""
        if (self.codes_rep_penalty != 1.0 and repetition_penalty == 1.0
                and getattr(self._cp, "generation_phase", None) == "codes"):
            repetition_penalty = self.codes_rep_penalty
        return super()._build_logits_processor(repetition_penalty)

    # ---- подсказка о длине генерации (чтобы выбрать ёмкость KV-кэша) ----
    def _generate_with_cfg_custom(self, *args: Any, **kwargs: Any):
        self._max_new_hint = kwargs.get("max_new_tokens", args[2] if len(args) > 2 else None)
        self._cp = kwargs.get("constrained_processor")
        try:
            return super()._generate_with_cfg_custom(*args, **kwargs)
        finally:
            self._cp = None

    def _generate_with_constrained_decoding(self, *args: Any, **kwargs: Any):
        self._max_new_hint = kwargs.get("max_new_tokens", args[2] if len(args) > 2 else None)
        self._cp = kwargs.get("constrained_processor")
        try:
            return super()._generate_with_constrained_decoding(*args, **kwargs)
        finally:
            self._cp = None

    # ---- прямой проход ----
    def _forward_pass(self, model, generated_ids, model_kwargs, past_key_values, use_cache):
        if self.cancel_check is not None:
            self.cancel_check()   # вызывается на каждом токене — отмена срабатывает за миллисекунды
        fast = self.fast
        if fast is None or not use_cache:
            return super()._forward_pass(model, generated_ids, model_kwargs, past_key_values, use_cache)
        if past_key_values is None:
            res = fast.prefill(generated_ids, model_kwargs.get("attention_mask"), self._max_new_hint or DEFAULT_MAX_NEW)
            if res is None:
                logger.debug("Быстрый декодер: запрос не помещается в кэш, штатный проход")
                return super()._forward_pass(model, generated_ids, model_kwargs, past_key_values, use_cache)
            logits, handle = res
            return SimpleNamespace(logits=logits, past_key_values=handle)
        if hasattr(past_key_values, "runner"):
            logits = fast.decode(past_key_values, generated_ids[:, -1:], codes=self._in_codes_phase())
            return SimpleNamespace(logits=logits, past_key_values=past_key_values)
        return super()._forward_pass(model, generated_ids, model_kwargs, past_key_values, use_cache)
