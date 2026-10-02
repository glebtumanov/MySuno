"""A/B-демонстрация для прослушивания: «до» (старые настройки) и «после» (режим «Максимум») на одних и тех же запросах.

python scripts/ab_demo.py   (сервер запущен) → треки в папке «Сравнение качества» в библиотеке.
До:    cfg LM 2.0, LM не переписывает caption, один вариант, без стража, как было при жалобе.
После: «Максимум» — 8 вариантов, автоматический выбор лучшего, cfg LM 4.0, cot_caption, страж.
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
FOLDER = "Сравнение качества"

def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def lab_prompts():
    sys.path.insert(0, __file__.rsplit("\\", 1)[0])
    import quality_lab  # noqa: PLC0415

    return quality_lab.PROMPTS


def wait(job_id, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = next((x for x in call("/api/jobs")["jobs"] if x["id"] == job_id), None)
        if j and j["status"] in ("done", "error", "cancelled"):
            return j, time.time() - t0
        time.sleep(1)
    raise TimeoutError(job_id)


def main() -> None:
    P = lab_prompts()
    cases = [
        ("Хаус·Техно·Эмбиент", dict(genres=["house", "techno", "ambient"], moods=["dark", "dreamy", "romantic"], vocal="none", prompt="", duration=180)),
        ("Фолк·Акустика", dict(genres=["folk", "acoustic"], moods=["romantic", "calm"], vocal="none", prompt="тёплая акустическая гитара, спокойная мелодия", duration=180)),
        ("Кино-оркестр", dict(genres=["cinematic"], moods=["epic"], vocal="none", prompt="эпическая оркестровая тема, струнные и медные", duration=180)),
        ("Поп-баллада (вокал)", dict(P["pop_f"], duration=120)),
        ("Рок (вокал)", dict(P["rock_m"], duration=120)),
    ]
    folders = call("/api/folders")["folders"]
    fid = next((f["id"] for f in folders if f["name"] == FOLDER), None) or call("/api/folders", "POST", {"name": FOLDER})["id"]
    for name, base in cases:
        for tag, over in (
            ("A до", dict(batch=1, rank=False, lm_cfg_scale=2.0, cot_caption=False, guard=False)),
            ("B после", dict(batch=8, rank=True, keep_all=False)),
        ):
            body = dict(base, title=f"{tag} · {name}", folder_id=fid, **over)
            job = call("/api/generate", "POST", body)
            j, dt = wait(job["id"])
            print(f"{tag:8} {name:22} {j['status']:6} {dt:6.0f} с  {j['error'] or ''}", flush=True)
    print("готово: треки в папке", FOLDER)


if __name__ == "__main__":
    main()
