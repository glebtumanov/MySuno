"""Бенчмарк скорости через HTTP (сервер запущен): python scripts/bench.py

Прогоняет несколько конфигураций на «тёплых» моделях и печатает время генерации.
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
LYRICS = ("[verse]\nДождь стучит по крыше,\nТихо город спит.\nЯ тебя услышу,\nСердце говорит.\n\n"
          "[chorus]\nОсень, осень, осень,\nТы со мной одна.\nПусть идут дожди,\nНе нужна весна.\n\n"
          "[verse]\nВетер гонит листья\nПо пустой стране,\nТы приснишься снова\nМне в ночной тишине.\n\n"
          "[chorus]\nОсень, осень, осень,\nТы со мной одна.\nПусть идут дожди,\nНе нужна весна.")


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def run(label, **kw):
    body = {"prompt": "лирическая поп-баллада", "genres": ["pop", "ballad"], "vocal": "female",
            "lyrics": LYRICS, "batch": 1, "title": f"bench {label}", **kw}
    t0 = time.time()
    job = call("/api/generate", "POST", body)
    while True:
        j = next(x for x in call("/api/jobs")["jobs"] if x["id"] == job["id"])
        if j["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.5)
    dt = time.time() - t0
    dur = body["duration"] * body["batch"]
    print(f"{label:38} {j['status']:6} {dt:6.1f} с  (x{dur / dt:4.1f} от реального времени)  {j['error'] or ''}", flush=True)
    return j


def cleanup() -> None:
    for t in call("/api/tracks?folder=all")["tracks"]:
        if t["title"].startswith("bench "):
            call(f"/api/tracks/{t['id']}", "DELETE")


if __name__ == "__main__":
    while call("/api/info")["engine"]["state"] in ("unloaded", "loading"):
        time.sleep(1)   # предзагрузка при старте сервера
    eng = call("/api/info")["engine"]
    print(f"модели готовы: загрузка {eng['load_seconds']} с, прогрев {eng['warmup_seconds']} с, план {eng['plan']}")
    run("warmup 15с", duration=15)
    for dur in (30, 120, 240):
        run(f"{dur}с  LM-планирование ВКЛ", duration=dur, thinking=True)
        run(f"{dur}с  LM-планирование ВЫКЛ", duration=dur, thinking=False)
    run("60с  2 варианта, LM вкл", duration=60, batch=2, thinking=True)
    run("60с  4 варианта, LM вкл", duration=60, batch=4, thinking=True)
    run("60с  4 варианта, LM выкл", duration=60, batch=4, thinking=False)
    cleanup()
