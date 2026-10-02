"""GPU-тест быстрого LM-декодера против эталона HF (пропускается без CUDA/весов/свободной видеопамяти).

Запуск: остановите сервер, затем  python -m unittest tests.test_fastlm_gpu -v
"""
import unittest

import torch

from mysuno import config

LM = config.CHECKPOINTS_DIR / "acestep-5Hz-lm-1.7B"


def _skip_reason() -> str | None:
    if not torch.cuda.is_available():
        return "нет CUDA"
    if not (LM / "config.json").exists():
        return "нет весов LM"
    free, _ = torch.cuda.mem_get_info()
    if free < 7 * 2**30:
        return "мало свободной видеопамяти (остановите сервер)"
    return None


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
class FastDecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        cls.tok = AutoTokenizer.from_pretrained(LM)
        cls.tok.padding_side = "left"
        cls.model = AutoModelForCausalLM.from_pretrained(LM, dtype=torch.bfloat16).cuda().eval()
        texts = ["<|im_start|>user\n# Caption\nsad piano ballad\n\n# Lyric\n[verse]\nДождь стучит по крыше\n"
                 "<|im_end|>\n<|im_start|>assistant\n<think>\nbpm: 72\n",
                 "<|im_start|>user\n# Caption\nNO USER INPUT\n<|im_end|>\n<|im_start|>assistant\n<think>\n"]
        enc = cls.tok(texts, return_tensors="pt", padding=True).to("cuda")
        cls.ids, cls.mask = enc["input_ids"], enc["attention_mask"]

    @classmethod
    def tearDownClass(cls):
        del cls.model
        torch.cuda.empty_cache()

    def test_prefill_and_decode_match_hf(self):
        from transformers import DynamicCache

        from mysuno.fastlm import FastQwen3

        steps = 16
        with torch.inference_mode():
            cache = DynamicCache()
            out = self.model(input_ids=self.ids, attention_mask=self.mask, past_key_values=cache, use_cache=True)
            ref = [out.logits[:, -1].float()]
            toks, mask = [ref[0].argmax(-1)], self.mask
            for _ in range(steps):
                mask = torch.cat([mask, torch.ones_like(mask[:, :1])], 1)
                out = self.model(input_ids=toks[-1][:, None], attention_mask=mask, past_key_values=cache, use_cache=True)
                ref.append(out.logits[:, -1].float())
                toks.append(ref[-1].argmax(-1))

            fast = FastQwen3(self.model)
            logits, handle = fast.prefill(self.ids, self.mask, max_new=steps + 4)
            got = [logits[:, -1].float()]
            for i in range(steps):
                got.append(fast.decode(handle, toks[i][:, None])[:, -1].float())

        agree = sum(int((a.argmax(-1) == b.argmax(-1)).all()) for a, b in zip(ref, got))
        self.assertGreaterEqual(agree, len(ref) - 2, "top-1 должен совпадать почти везде")
        for a, b in zip(ref, got):
            self.assertLess((a - b).abs().mean().item(), 0.6, "средняя ошибка логитов в пределах шума bf16")

    def test_codes_mode_masks_everything_outside_range(self):
        from mysuno.fastlm import FastQwen3

        lo = self.tok.convert_tokens_to_ids("<|audio_code_0|>")
        hi = self.tok.convert_tokens_to_ids("<|audio_code_63999|>") + 1
        eos = self.tok.eos_token_id
        with torch.inference_mode():
            fast = FastQwen3(self.model)
            fast.set_code_range(lo, hi, extra=[eos])
            logits, handle = fast.prefill(self.ids, self.mask, max_new=8)
            tok = torch.full((2, 1), lo + 5, device="cuda")
            full = fast.decode(handle, tok, codes=False)[:, -1].float()
            codes = fast.decode(handle, tok, codes=True)[:, -1].float()
        allowed = torch.zeros(codes.shape[-1], dtype=torch.bool, device="cuda")
        allowed[lo:hi] = True
        allowed[eos] = True
        self.assertTrue(torch.isinf(codes[:, ~allowed]).all() and (codes[:, ~allowed] < 0).all())
        self.assertTrue(torch.isfinite(codes[:, allowed]).all(), "EOS и коды должны иметь конечные логиты (иначе NaN в CFG)")
        self.assertEqual(codes.shape, full.shape)


if __name__ == "__main__":
    unittest.main()
