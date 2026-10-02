"""Качество быстрого декодера на реалистичной фазе кодов (с CFG): python scripts/check_fastlm_codes.py [--steps 300]

Teacher forcing: токены сэмплируются из распределения HF bf16 (CFG=2, T=0.85), их же получают быстрый декодер и fp32-эталон.
Сравниваются распределения по аудио-кодам: KL(эталон || оценка) и совпадение top-1 с fp32-эталоном.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, DynamicCache  # noqa: E402

from acestep.llm_inference import LLMHandler  # noqa: E402
from mysuno import config  # noqa: E402
from mysuno.fastlm import FastQwen3  # noqa: E402

COT = ("<think>\nbpm: 76\ncaption: A lyrical pop ballad with soft female vocals, piano and strings.\nduration: 120\n"
       "genres: pop, ballad\nkeyscale: A minor\nlanguage: ru\ntimesignature: 4\n</think>\n")
LYRICS = "[verse]\nДождь стучит по крыше,\nТихо город спит.\n\n[chorus]\nОсень, осень, осень,\nТы со мной одна."
CFG, TEMP = 2.0, 0.85


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lm", default="acestep-5Hz-lm-1.7B")
    ap.add_argument("--steps", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    torch.manual_seed(args.seed)

    path = config.CHECKPOINTS_DIR / args.lm
    tok = AutoTokenizer.from_pretrained(path)
    tok.padding_side = "left"
    h = LLMHandler()
    h.llm_tokenizer = tok
    cond = h.build_formatted_prompt_with_cot("lyrical pop ballad, soft female vocals", LYRICS, COT)
    uncond = h.build_formatted_prompt_with_cot("lyrical pop ballad, soft female vocals", LYRICS, COT, is_negative_prompt=True)
    enc = tok([cond, uncond], return_tensors="pt", padding=True).to("cuda")
    ids, mask = enc["input_ids"], enc["attention_mask"]
    lo = tok.convert_tokens_to_ids("<|audio_code_0|>")
    hi = tok.convert_tokens_to_ids("<|audio_code_63999|>") + 1
    print(f"prompt B=2 L={ids.shape[1]} (паддинг {(mask == 0).sum(1).tolist()}), коды {lo}..{hi}")

    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).cuda().eval()
    ref32 = AutoModelForCausalLM.from_pretrained(path, dtype=torch.float32).cuda().eval()

    def cfg_dist(logits2):  # [2,V] → распределение по кодам после CFG и температуры
        c, u = logits2[0, lo:hi].float(), logits2[1, lo:hi].float()
        return torch.log_softmax((u + CFG * (c - u)) / TEMP, -1)

    with torch.inference_mode():
        # HF bf16 и fp32 (teacher forcing их токенами HF)
        c16, c32 = DynamicCache(), DynamicCache()
        o16 = model(input_ids=ids, attention_mask=mask, past_key_values=c16, use_cache=True)
        o32 = ref32(input_ids=ids, attention_mask=mask, past_key_values=c32, use_cache=True)
        lp16, lp32 = [cfg_dist(o16.logits[:, -1])], [cfg_dist(o32.logits[:, -1])]
        toks = []
        cur_mask = mask
        for _ in range(args.steps):
            t = torch.multinomial(lp16[-1].exp(), 1)[0] + lo           # токен из распределения HF bf16
            toks.append(t)
            tt = t.view(1, 1).repeat(2, 1)
            cur_mask = torch.cat([cur_mask, torch.ones_like(cur_mask[:, :1])], 1)
            o16 = model(input_ids=tt, attention_mask=cur_mask, past_key_values=c16, use_cache=True)
            o32 = ref32(input_ids=tt, attention_mask=cur_mask, past_key_values=c32, use_cache=True)
            lp16.append(cfg_dist(o16.logits[:, -1]))
            lp32.append(cfg_dist(o32.logits[:, -1]))
        del ref32, c32
        torch.cuda.empty_cache()

        # быстрый декодер в режиме кодов (как в боевом пути)
        fast = FastQwen3(model)
        fast.set_code_range(lo, hi, extra=[tok.eos_token_id])
        logits, handle = fast.prefill(ids, mask, max_new=args.steps + 8)
        lpf = [cfg_dist(logits[:, -1])]
        for t in toks:
            lg = fast.decode(handle, t.view(1, 1).repeat(2, 1), codes=True)
            lpf.append(cfg_dist(lg[:, -1]))

    def kl(p_log, q_log):  # KL(p||q)
        return (p_log.exp() * (p_log - q_log)).sum().item()

    n = len(lp32)
    print("\nKL к fp32 по позиции (среднее в окне шагов):")
    bucket = max(1, args.steps // 6)
    for name, lps in (("HF bf16", lp16), ("быстрый", lpf)):
        kls = [kl(a, b) for a, b in zip(lp32, lps)]
        print(f"  {name:8} " + "  ".join(f"{i}-{i + bucket}: {sum(kls[i:i + bucket]) / len(kls[i:i + bucket]):.4f}"
                                          for i in range(0, n, bucket)))
    print()
    for name, lps in (("HF bf16", lp16), ("быстрый", lpf)):
        kls = [kl(a, b) for a, b in zip(lp32, lps)]
        top1 = sum(int(a.argmax() == b.argmax()) for a, b in zip(lp32, lps)) / n
        tv = sum(0.5 * (a.exp() - b.exp()).abs().sum().item() for a, b in zip(lp32, lps)) / n
        lpt = sum(lps[i][toks[i] - lo].item() for i in range(n - 1)) / (n - 1)
        print(f"{name:8} к fp32: KL mean {sum(kls) / n:.5f} max {max(kls):.4f} | TV mean {tv:.4f} | top-1 {top1:.1%} | "
              f"ср. log-prob выбранных токенов {lpt:.3f}")
    ref_lpt = sum(lp32[i][toks[i] - lo].item() for i in range(n - 1)) / (n - 1)
    print(f"fp32 ср. log-prob тех же токенов: {ref_lpt:.3f}; шагов: {n}")


if __name__ == "__main__":
    main()
