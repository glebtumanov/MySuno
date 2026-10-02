"""Сверка быстрого декодера с эталоном HF и замер скорости: python scripts/check_fastlm.py [--lm acestep-5Hz-lm-1.7B]

Нужна свободная GPU (остановите сервер). Проверяет: логиты prefill и пошагового декодирования (teacher forcing)
на батче с левым паддингом, затем время шага для HF eager и CUDA Graph.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer, DynamicCache  # noqa: E402

from mysuno import config  # noqa: E402
from mysuno.fastlm import FastQwen3  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lm", default="acestep-5Hz-lm-1.7B")
    ap.add_argument("--steps", type=int, default=32)
    ap.add_argument("--fp32", action="store_true", help="сравнить с fp32-эталоном (нужно ~8 ГБ VRAM)")
    ap.add_argument("--no-bench", action="store_true")
    args = ap.parse_args()

    path = config.CHECKPOINTS_DIR / args.lm
    tok = AutoTokenizer.from_pretrained(path)
    tok.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).cuda().eval()

    texts = ["<|im_start|>user\n# Caption\nsad piano ballad with strings and soft female vocals\n\n# Lyric\n"
             "[verse]\nДождь стучит по крыше, тихо город спит\n<|im_end|>\n<|im_start|>assistant\n<think>\nbpm: 72\n",
             "<|im_start|>user\n# Caption\nNO USER INPUT\n<|im_end|>\n<|im_start|>assistant\n<think>\n"]
    enc = tok(texts, return_tensors="pt", padding=True).to("cuda")
    ids, mask = enc["input_ids"], enc["attention_mask"]
    print(f"prompt: B={ids.shape[0]} L={ids.shape[1]}, паддинг: {(mask == 0).sum(1).tolist()}")

    with torch.inference_mode():
        # ---- эталон HF
        cache = DynamicCache()
        ref = model(input_ids=ids, attention_mask=mask, past_key_values=cache, use_cache=True)
        ref_logits = [ref.logits[:, -1].float()]
        cur_ids, cur_mask = ids, mask
        tokens = [ref_logits[0].argmax(-1)]
        for _ in range(args.steps):
            cur_mask = torch.cat([cur_mask, torch.ones_like(cur_mask[:, :1])], 1)
            out = model(input_ids=tokens[-1][:, None], attention_mask=cur_mask, past_key_values=cache, use_cache=True)
            ref_logits.append(out.logits[:, -1].float())
            tokens.append(ref_logits[-1].argmax(-1))

        # ---- быстрый декодер
        fast = FastQwen3(model)
        torch.cuda.synchronize()
        t0 = time.time()
        logits, handle = fast.prefill(ids, mask, max_new=args.steps + 8)
        torch.cuda.synchronize()
        print(f"prefill + захват графа: {time.time() - t0:.2f} с")
        got = [logits[:, -1].float()]
        for i in range(args.steps):
            got.append(fast.decode(handle, tokens[i][:, None])[:, -1].float())

    worst, agree = 0.0, 0
    for i, (a, b) in enumerate(zip(ref_logits, got)):
        diff = (a - b).abs().max().item()
        worst = max(worst, diff)
        agree += int((a.argmax(-1) == b.argmax(-1)).all().item())
        if i in (0, 1, args.steps):
            print(f"шаг {i:2}: max|Δlogit| = {diff:.3f} (диапазон логитов ±{a.abs().max().item():.1f}), top1 совпадает: "
                  f"{(a.argmax(-1) == b.argmax(-1)).tolist()}")
    n = len(ref_logits)
    print(f"ИТОГ: худшее |Δ| = {worst:.3f}; top-1 совпал на {agree}/{n} шагах")

    # ---- кто ближе к «истине»: эталон fp32 (те же токены, teacher forcing)
    if args.fp32:
        ref32 = AutoModelForCausalLM.from_pretrained(path, dtype=torch.float32).cuda().eval()
        with torch.inference_mode():
            cache32 = DynamicCache()
            r0 = ref32(input_ids=ids, attention_mask=mask, past_key_values=cache32, use_cache=True)
            truth = [r0.logits[:, -1].float()]
            m32 = mask
            for i in range(args.steps):
                m32 = torch.cat([m32, torch.ones_like(m32[:, :1])], 1)
                o = ref32(input_ids=tokens[i][:, None], attention_mask=m32, past_key_values=cache32, use_cache=True)
                truth.append(o.logits[:, -1].float())
        del ref32, cache32
        torch.cuda.empty_cache()

        def stats(xs):
            d = torch.stack([(x - t).abs() for x, t in zip(xs, truth)])        # [steps,B,V]
            return d.max().item(), d.mean().item(), d.amax(-1).amax(0).tolist()

        for name, xs in (("HF bf16     ", ref_logits), ("быстрый     ", got)):
            mx, mean, per_row = stats(xs)
            print(f"Δ к fp32-эталону, {name}: max {mx:.3f}  mean {mean:.4f}  по строкам батча (max): "
                  f"{[round(v, 2) for v in per_row]}")
            # где худшая точка и насколько она важна (вероятностная масса токена по эталону)
            worst = max(((i, b, (x[b] - t[b]).abs().argmax().item(), (x[b] - t[b]).abs().max().item())
                         for i, (x, t) in enumerate(zip(xs, truth)) for b in range(x.shape[0])), key=lambda r: r[3])
            i, b, tokid, d = worst
            p = torch.softmax(truth[i][b], -1)[tokid].item()
            print(f"   худшая точка: шаг {i}, строка {b}, токен {tokid}: Δ={d:.2f}, эталон={truth[i][b][tokid]:.2f}, "
                  f"оценка={xs[i][b][tokid]:.2f}, вероятность токена по эталону={p:.2e}; "
                  f"max логит эталона на шаге={truth[i][b].max():.2f}")
            # ошибка в «значимой зоне»: токены с логитом в пределах 10 от максимума
            errs = []
            for x, t in zip(xs, truth):
                for b in range(x.shape[0]):
                    near = t[b] > t[b].max() - 10
                    errs.append((x[b][near] - t[b][near]).abs())
            e = torch.cat(errs)
            print(f"   в значимой зоне (логит ≥ max−10): mean {e.mean().item():.4f}  p99 {e.quantile(0.99).item():.3f}  "
                  f"max {e.max().item():.3f}")

    if args.no_bench:
        return

    # ---- скорость
    with torch.inference_mode():
        def bench(fn, n=60):
            for _ in range(5):
                fn()
            torch.cuda.synchronize()
            t = time.time()
            for _ in range(n):
                fn()
            torch.cuda.synchronize()
            return (time.time() - t) / n * 1000

        cache2 = DynamicCache()
        model(input_ids=ids, attention_mask=mask, past_key_values=cache2, use_cache=True)
        m2 = mask
        tk = tokens[0][:, None]

        def hf_step():
            nonlocal m2
            m2 = torch.cat([m2, torch.ones_like(m2[:, :1])], 1)
            model(input_ids=tk, attention_mask=m2, past_key_values=cache2, use_cache=True)

        r = fast._runner(2, 2048)
        fast._prefill(r, ids, mask.bool())
        h = type("H", (), {"runner": r, "pos": ids.shape[1]})()

        def fast_step():
            h.pos = ids.shape[1] + 10  # позицию не двигаем: измеряем только стоимость шага
            fast.decode(h, tk)

        print(f"\nшаг декодирования (B=2): HF eager {bench(hf_step):.1f} мс | CUDA Graph {bench(fast_step):.1f} мс")


if __name__ == "__main__":
    main()
