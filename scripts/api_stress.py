"""Стресс-тест движка по HTTP (сервер запущен): python scripts/api_stress.py

Проверяет: предзагрузку, батчи, длинные треки, автотекст, тихий режим без LM, отмену посреди генерации и то,
что после отмены движок продолжает работать. Тестовые треки удаляются в конце.
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
LYRICS = ("[verse]\nДождь стучит по крыше,\nТихо город спит.\nЯ тебя услышу,\nСердце говорит.\n\n"
          "[chorus]\nОсень, осень, осень,\nТы со мной одна.\nПусть идут дожди,\nНе нужна весна.")
created: list[str] = []
failures: list[str] = []


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def vram() -> str:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"], capture_output=True, text=True)
    return out.stdout.strip()


def wait_job(job_id, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = next((x for x in call("/api/jobs")["jobs"] if x["id"] == job_id), None)
        if j and j["status"] in ("done", "error", "cancelled"):
            return j, time.time() - t0
        time.sleep(0.4)
    raise TimeoutError(job_id)


def check(name, cond, extra=""):
    print(f"  {'OK ' if cond else 'FAIL'} {name} {extra}")
    if not cond:
        failures.append(name)


def gen(label, **kw):
    body = {"prompt": "лирическая поп-баллада про осень", "genres": ["pop", "ballad"], "vocal": "female",
            "lyrics": LYRICS, "batch": 1, "title": f"stress {label}", **kw}
    job = call("/api/generate", "POST", body)
    j, dt = wait_job(job["id"])
    created.extend(j["track_ids"])
    print(f"{label:38} {j['status']:9} {dt:6.1f} с  треков: {len(j['track_ids'])}  {j['error'] or ''}", flush=True)
    return j, dt


def main():
    print("== предзагрузка ==")
    t0 = time.time()
    while call("/api/info")["engine"]["state"] in ("unloaded", "loading") and time.time() - t0 < 180:
        time.sleep(1)
    eng = call("/api/info")["engine"]
    check("модели готовы после старта", eng["state"] == "ready", f"(загрузка {eng['load_seconds']} с, прогрев {eng['warmup_seconds']} с, "
          f"fast_lm={eng['plan']['fast_lm']}, VRAM {vram()})")

    print("== генерации ==")
    j, dt = gen("30с с вокалом", duration=30)
    check("30 с", j["status"] == "done")
    j, dt = gen("120с батч 2 (LM)", duration=120, batch=2)
    check("120 с x2", j["status"] == "done" and len(j["track_ids"]) == 2)
    j, dt = gen("60с батч 4 (LM)", duration=60, batch=4)
    check("60 с x4", j["status"] == "done" and len(j["track_ids"]) == 4)
    j, dt = gen("300с (LM)", duration=300)
    check("300 с", j["status"] == "done")
    j, dt = gen("45с автотекст (create_sample)", duration=45, lyrics="")
    tr = call("/api/tracks?folder=all")["tracks"]
    got = next((t for t in tr if t["id"] in j["track_ids"]), None)
    check("автотекст", j["status"] == "done" and bool(got and got["lyrics"].strip()), f"(строк текста: {len((got or {}).get('lyrics', '').splitlines())})")
    j, dt = gen("120с без LM-планирования", duration=120, thinking=False)
    check("без LM", j["status"] == "done")
    j, dt = gen("30с инструментал", duration=30, vocal="none", lyrics="")
    check("инструментал", j["status"] == "done")

    print("== отмена ==")
    job = call("/api/generate", "POST", {"prompt": "эпическая музыка", "genres": ["cinematic"], "vocal": "none",
                                         "duration": 240, "title": "stress cancel"})
    while True:
        j = next(x for x in call("/api/jobs")["jobs"] if x["id"] == job["id"])
        if j["status"] == "running" and j["progress"] > 0.28:
            break
        time.sleep(0.2)
    time.sleep(1.5)
    t_c = time.time()
    resp = call(f"/api/jobs/{job['id']}", "DELETE")
    j, _ = wait_job(job["id"], timeout=60)
    check("отмена выполняющейся", j["status"] == "cancelled", f"(действие {resp['action']}, остановилась за {time.time() - t_c:.1f} с)")
    j, dt = gen("после отмены 30с", duration=30)
    check("движок работает после отмены", j["status"] == "done")

    print("== итог ==")
    eng = call("/api/info")["engine"]
    print("движок:", eng["state"], "fast_lm:", eng["plan"]["fast_lm"], "| VRAM", vram())
    for tid in created:
        try:
            call(f"/api/tracks/{tid}", "DELETE")
        except Exception:
            pass
    print("удалено тестовых треков:", len(created))
    print("ПРОВАЛЕНО:" if failures else "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ", failures or "")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
