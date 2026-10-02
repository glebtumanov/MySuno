"""Лаборатория качества: python scripts/quality_lab.py run|score|report ...

  run    --exps a,b,c [--prompts cine,techno,...] [--seeds 1,2] [--duration 90]   генерирует (сервер остановить!)
  score  [--device cpu|cuda]                                                       оценивает новые файлы (Audiobox + эвристики)
  report [--by-prompt]                                                             сводная таблица по конфигурациям

Файлы: data/lab/<exp>/*.flac, data/lab/results.jsonl (генерации), data/lab/scores.jsonl (оценки).
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mysuno import config  # noqa: E402

LAB = config.DATA / "lab"
RESULTS, SCORES = LAB / "results.jsonl", LAB / "scores.jsonl"

LY_POP = """[Intro - piano]

[Verse 1]
Осень стучится в окно
Тихо, как старый друг
Город укрыт серым сном
Дождь рисует круг

[Chorus - emotional]
Я помню твои глаза
Тёплые, как свет
Пусть уходит осень прочь
Только твой след

[Verse 2]
Листья летят над землёй
Кружатся и молчат
Время спешит за окном
Сердце моё не спит

[Chorus - emotional]
Я помню твои глаза
Тёплые, как свет
Пусть уходит осень прочь
Только твой след

[Outro - fade out]"""

LY_ROCK = """[Verse 1]
Дорога летит вперёд
Ветер в лицо стучит
Мотор поёт свою песню
Сердце в груди горит

[Chorus - anthemic]
Мы не вернёмся назад
Небо над нами огонь
Мы выбираем свободу
Громче, громче, вперёд

[Verse 2]
Город остался за спиной
Звёзды зовут за собой
Пусть впереди неизвестность
Мы идём на свой бой

[Chorus - anthemic]
Мы не вернёмся назад
Небо над нами огонь
Мы выбираем свободу
Громче, громче, вперёд"""

PROMPTS = {
    "cine": dict(genres=["cinematic"], vocal="none", prompt="эпическая оркестровая тема, струнные и медные, нарастающее напряжение"),
    "techno": dict(genres=["techno"], vocal="none", prompt="гипнотический клубный трек, глубокий бас, плавное нарастание"),
    "folk": dict(genres=["folk", "acoustic"], vocal="none", prompt="тёплая акустическая гитара, спокойная мелодия у костра"),
    "jazz": dict(genres=["jazz"], vocal="none", prompt="вечерний джаз, фортепиано, контрабас, щёточные барабаны"),
    "pop_f": dict(genres=["pop", "ballad"], vocal="female", prompt="лирическая песня про осенний дождь, пианино и струнные", lyrics=LY_POP),
    "rock_m": dict(genres=["rock"], vocal="male", prompt="энергичная песня про дорогу и свободу, гитарный драйв", lyrics=LY_ROCK),
    # проверка негативных жанров: «рок» без металла/панка (оценивается отдельным скриптом neg_eval.py)
    "neg_rock": dict(genres=["rock"], vocal="none", prompt="энергичная гитарная музыка"),
    # «грязные» наборы, как у реального пользователя: несколько жанров и противоречивых настроений без описания
    "mess_house": dict(genres=["house", "techno", "ambient"], moods=["dark", "dreamy", "romantic"], vocal="none", prompt=""),
    "mess_folk": dict(genres=["folk", "acoustic", "jazz"], moods=["romantic", "epic", "calm"], vocal="none", prompt=""),
    "mess_cine": dict(genres=["cinematic", "synthwave"], moods=["epic", "calm", "sad"], vocal="none", prompt=""),
}

TURBO = dict(dit_model="acestep-v15-turbo", lm_model="acestep-5Hz-lm-1.7B", fast_lm=True)
SFT = dict(dit_model="acestep-v15-sft", lm_model="acestep-5Hz-lm-1.7B", fast_lm=True)
XLT = dict(TURBO, dit_model="acestep-v15-xl-turbo", offload="on", fast_lm=False)
XLS = dict(SFT, dit_model="acestep-v15-xl-sft", offload="on", fast_lm=False)
Q = dict(steps=50, guidance=7.0, shift=3.0)
C = dict(cot_caption=True)      # LM переписывает caption (по результатам эксперимента 1–2: +0.5 на «грязных» наборах)

# имя → (settings, req-переопределения)
EXPS = {
    # --- регресс быстрого декодера и влияние переписывания caption (эксперименты 1–2)
    "turbo_fast": (TURBO, {}),
    "turbo_stock": (dict(TURBO, fast_lm=False), {}),
    "turbo_cotcap": (TURBO, dict(C)),
    # --- DiT (все с cot_caption)
    "turbo_nolm": (dict(TURBO, lm_model="none"), dict(thinking=False)),
    "sft_c": (SFT, dict(Q, **C)),
    "sft_adg_c": (SFT, dict(Q, adg=True, **C)),
    "sft_g9_c": (SFT, dict(Q, guidance=9.0, **C)),
    "sft_g4_c": (SFT, dict(Q, guidance=4.0, **C)),
    "sft_nolm": (dict(SFT, lm_model="none"), dict(Q, thinking=False)),
    "xlturbo_c": (XLT, dict(C)),
    "xlsft_c": (XLS, dict(Q, **C)),
    "xlsft_adg_c": (XLS, dict(Q, adg=True, **C)),
    # --- LM (с лучшим DiT; подставьте нужный)
    "sft_lm4b_c": (dict(SFT, lm_model="acestep-5Hz-lm-4B", offload="on", fast_lm=False), dict(Q, **C)),
    "sft_lm06_c": (dict(SFT, lm_model="acestep-5Hz-lm-0.6B"), dict(Q, **C)),
    # --- сэмплирование LM (turbo — быстрый прогон; смотрим на зацикливание кодов)
    "turbo_t07_c": (TURBO, dict(C, temperature=0.7)),
    "turbo_t10_c": (TURBO, dict(C, temperature=1.0, lm_top_p=0.95)),
    "turbo_rep105_c": (TURBO, dict(C, lm_rep_penalty=1.05)),
    "turbo_rep110_c": (TURBO, dict(C, lm_rep_penalty=1.10)),
    "turbo_cfg3_c": (TURBO, dict(C, lm_cfg_scale=3.0)),
    "turbo_cfg4_c": (TURBO, dict(C, lm_cfg_scale=4.0)),
    "turbo_cfg5_c": (TURBO, dict(C, lm_cfg_scale=5.0)),
    "turbo_cfg7_c": (TURBO, dict(C, lm_cfg_scale=7.0)),
    "turbo_cfg4_t10_c": (TURBO, dict(C, lm_cfg_scale=4.0, temperature=1.0, lm_top_p=0.95)),
    "turbo_cfg4_rep_c": (TURBO, dict(C, lm_cfg_scale=4.0, lm_rep_penalty=1.05)),
    "turbo_lm4b_cfg4_c": (dict(TURBO, lm_model="acestep-5Hz-lm-4B", offload="on", fast_lm=False), dict(C, lm_cfg_scale=4.0)),
    "turbo_lm06_cfg4_c": (dict(TURBO, lm_model="acestep-5Hz-lm-0.6B"), dict(C, lm_cfg_scale=4.0)),
    "neg_cfg2": (TURBO, dict(C, lm_cfg_scale=2.0, negative_genres=["metal", "hardrock", "punk"])),
    "neg_cfg5": (TURBO, dict(C, lm_cfg_scale=5.0, negative_genres=["metal", "hardrock", "punk"])),
    "pos_cfg2": (TURBO, dict(C, lm_cfg_scale=2.0)),
    "pos_cfg5": (TURBO, dict(C, lm_cfg_scale=5.0)),
    # --- сэмплер DiT (turbo)
    "turbo_s12_c": (TURBO, dict(C, steps=12)),
    "turbo_s16_c": (TURBO, dict(C, steps=16)),
    "turbo_heun_c": (TURBO, dict(C, sampler="heun")),
    "turbo_sde_c": (TURBO, dict(C, infer_method="sde")),
    "turbo_nodcw_c": (TURBO, dict(C, dcw=False)),
    # --- варианты turbo (скачать: scripts/download_models.py acestep-v15-turbo-shift3 acestep-v15-turbo-continuous)
    "turbo3_c": (dict(TURBO, dit_model="acestep-v15-turbo-shift3"), dict(C)),
    "turbocont_c": (dict(TURBO, dit_model="acestep-v15-turbo-continuous"), dict(C)),
    "turbo1_c": (dict(TURBO, dit_model="acestep-v15-turbo-shift1"), dict(C)),
}


def log(rec: dict, path: Path) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def cmd_run(args) -> None:
    import torch

    from mysuno.engine import get_engine

    eng = get_engine()
    prompts = [p for p in PROMPTS if not p.startswith(("mess_", "neg_"))] if args.prompts == "all" else \
        (list(PROMPTS) if args.prompts == "every" else args.prompts.split(","))
    seeds = [int(s) for s in args.seeds.split(",")]
    done = {(r["exp"], r["prompt"], r["seed"], r["duration"]) for r in read(RESULTS)}
    for name in args.exps.split(","):
        settings_over, req_over = EXPS[name]
        settings = config.load_settings()
        settings.update(settings_over)
        out_dir = LAB / name
        out_dir.mkdir(parents=True, exist_ok=True)
        for pid in prompts:
            for seed in seeds:
                dur = args.duration
                if (name, pid, seed, dur) in done:
                    continue
                req = {**PROMPTS[pid], "duration": dur, "seed": seed, "batch": 1, "format": "flac", "temperature": 0.85,
                       **req_over}
                torch.manual_seed(seed)
                t0 = time.time()
                try:
                    out = eng.generate(req, settings, lambda v, d: None, config.TMP_DIR / "lab")
                except Exception as exc:  # noqa: BLE001
                    print(f"{name:14} {pid:7} s{seed} ОШИБКА: {str(exc)[:150]}", flush=True)
                    continue
                dst = out_dir / f"{pid}_d{int(dur)}_s{seed}.flac"
                shutil.move(out["tracks"][0]["path"], dst)
                log({"exp": name, "prompt": pid, "seed": seed, "duration": dur, "path": str(dst),
                     "gen_seconds": out["gen_seconds"], "plan": out["plan"], "caption": out["caption"],
                     "time_costs": {k: round(v, 2) for k, v in out["time_costs"].items() if isinstance(v, (int, float))}},
                    RESULTS)
                print(f"{name:14} {pid:7} s{seed} d{dur:.0f}  {time.time() - t0:6.1f} с  plan dit={out['plan']['dit'][13:]} "
                      f"lm={(out['plan']['lm'] or '-')[-4:]} fast={out['plan'].get('fast_lm')}", flush=True)
        eng.unload()
    print("готово")


def cmd_score(args) -> None:
    from mysuno import quality

    scored = {r["path"] for r in read(SCORES)}
    todo = [r for r in read(RESULTS) if r["path"] not in scored and Path(r["path"]).exists()]
    print(f"к оценке: {len(todo)}", flush=True)
    for r in todo:
        s = quality.score_file(r["path"], device=args.device)
        s.update({"path": r["path"], "overall": quality.overall(s)})
        if args.clap:
            s["clap"] = quality.clap_similarity(r["path"], r["caption"], device=args.device)
        log(s, SCORES)
        print(f"{r['exp']:14} {r['prompt']:7} s{r['seed']}  CE {s['CE']:.2f} PQ {s['PQ']:.2f} p10 {s['CE_p10']:.2f} "
              f"шум {s['noise_ratio'] * 100:3.0f}% стат {s['static'] * 100:3.0f}%  итог {s['overall']:.2f}"
              + (f"  clap {s['clap']:.3f}" if "clap" in s else ""), flush=True)


def cmd_clap(args) -> None:
    """Дописывает CLAP-близость к уже оценённым файлам (scores.jsonl перезаписывается)."""
    from mysuno import quality

    res = {r["path"]: r for r in read(RESULTS)}
    scores = read(SCORES)
    n = 0
    for s in scores:
        if "clap" not in s and s["path"] in res and Path(s["path"]).exists():
            s["clap"] = quality.clap_similarity(s["path"], res[s["path"]]["caption"], device=args.device)
            n += 1
            if n % 20 == 0:
                print(f"clap: {n}", flush=True)
    SCORES.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in scores), encoding="utf-8")
    print(f"clap дописан для {n} файлов")


def cmd_report(args) -> None:
    import numpy as np

    res = {r["path"]: r for r in read(RESULTS)}
    rows = [(res[s["path"]], s) for s in read(SCORES) if s["path"] in res]
    keyf = (lambda r: (r["exp"], r["prompt"])) if args.by_prompt else (lambda r: (r["exp"], ""))
    groups: dict = {}
    for r, s in rows:
        if args.duration and r["duration"] != args.duration:
            continue
        groups.setdefault(keyf(r), []).append((r, s))
    print(f"{'конфигурация':16} {'промпт':9} {'n':>3} {'CE':>5} {'PQ':>5} {'CEp10':>6} {'шум%':>5} {'стат%':>5} {'итог':>6} {'clap':>6} {'время,с':>8}")
    for (exp, pid), items in sorted(groups.items(), key=lambda kv: -np.mean([s["overall"] for _, s in kv[1]])):
        m = lambda k: float(np.mean([s[k] for _, s in items]))  # noqa: E731
        t = float(np.mean([r["gen_seconds"] for r, _ in items]))
        claps = [s["clap"] for _, s in items if "clap" in s]
        clap = float(np.mean(claps)) if claps else float("nan")
        print(f"{exp:16} {pid:9} {len(items):3} {m('CE'):5.2f} {m('PQ'):5.2f} {m('CE_p10'):6.2f} {m('noise_ratio') * 100:5.0f} "
              f"{m('static') * 100:5.0f} {m('overall'):6.2f} {clap:6.3f} {t:8.1f}")


def cmd_bon(args) -> None:
    """Выигрыш от «лучший из N»: бутстрэп по готовым оценкам одной конфигурации (по каждому промпту отдельно)."""
    import numpy as np

    res = {r["path"]: r for r in read(RESULTS)}
    rng = np.random.default_rng(0)
    for exp in args.exps.split(","):
        by_prompt: dict[str, list[tuple[float, float]]] = {}
        for s in read(SCORES):
            r = res.get(s["path"])
            if r and r["exp"] == exp and (not args.duration or r["duration"] == args.duration):
                sel = s["overall"] + args.clap_w * s.get("clap", 0.0)
                by_prompt.setdefault(r["prompt"], []).append((sel, s["overall"]))
        if not by_prompt:
            continue
        print(f"\n{exp}: промптов {len(by_prompt)}, вариантов на промпт {[len(v) for v in by_prompt.values()]}")
        for n in (1, 2, 4, 8):
            gains, picked = [], []
            for vals in by_prompt.values():
                if len(vals) < n:
                    continue
                arr = np.array(vals)
                for _ in range(400):
                    idx = rng.choice(len(arr), n, replace=False)
                    best = idx[np.argmax(arr[idx, 0])]
                    picked.append(arr[best, 1])
                gains.append(np.mean(arr[:, 1]))
            if picked:
                print(f"  N={n}: средний итог лучшего = {np.mean(picked):.3f}  (при N=1 в среднем {np.mean(gains):.3f})")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bon")
    b.add_argument("--exps", required=True)
    b.add_argument("--duration", type=float, default=0)
    b.add_argument("--clap-w", type=float, default=5.0)
    r = sub.add_parser("run")
    r.add_argument("--exps", required=True)
    r.add_argument("--prompts", default="all")
    r.add_argument("--seeds", default="1,2")
    r.add_argument("--duration", type=float, default=90)
    s = sub.add_parser("score")
    s.add_argument("--device", default="cpu")
    s.add_argument("--clap", action="store_true", help="заодно считать CLAP-близость к описанию")
    c = sub.add_parser("clap")
    c.add_argument("--device", default="cpu")
    p = sub.add_parser("report")
    p.add_argument("--by-prompt", action="store_true")
    p.add_argument("--duration", type=float, default=0)
    args = ap.parse_args()
    LAB.mkdir(parents=True, exist_ok=True)
    {"run": cmd_run, "score": cmd_score, "clap": cmd_clap, "report": cmd_report, "bon": cmd_bon}[args.cmd](args)


if __name__ == "__main__":
    main()
