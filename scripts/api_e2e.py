"""Сквозной тест по HTTP (сервер должен быть запущен): python scripts/api_e2e.py [--seconds 30]

Создаёт задачи через /api/generate, ждёт результат, проверяет аудио (в т.ч. Range) и печатает тайминги.
"""
import argparse
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return r, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)


def run_job(body, label):
    t0 = time.time()
    _, job = call("/api/generate", "POST", body)
    last = ""
    while True:
        _, data = call("/api/jobs")
        j = next(x for x in data["jobs"] if x["id"] == job["id"])
        line = f"[{j['status']:8}] {j['progress'] * 100:5.1f}%  {j['stage']}"
        if line != last:
            print(f"  {time.time() - t0:6.1f}s {line}")
            last = line
        if j["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(1)
    print(f"{label}: {j['status']} за {time.time() - t0:.1f} с")
    if j["status"] != "done":
        print("  ошибка:", j["error"])
        sys.exit(1)
    return j


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--no-lm", action="store_true", help="LM-планирование выключено (thinking=false)")
    args = ap.parse_args()

    body = {
        "title": "E2E тест",
        "prompt": "грустная лирическая песня про осенний дождь, пианино и струнные",
        "genres": ["pop", "ballad"], "moods": ["sad"], "negative_genres": ["metal"],
        "vocal": "female",
        "lyrics": "[verse]\nДождь стучит по крыше,\nТихо город спит.\nЯ тебя услышу,\nСердце говорит.\n\n[chorus]\nОсень, осень, осень,\nТы со мной одна.\nПусть идут дожди,\nНе нужна весна.",
        "duration": args.seconds, "temperature": 0.85, "batch": 1, "thinking": not args.no_lm,
    }
    print("== 1-я генерация (с загрузкой моделей) ==")
    j1 = run_job(body, "1")
    print("== 2-я генерация (модели уже в памяти) ==")
    j2 = run_job({**body, "title": "E2E тест 2"}, "2")

    for tid in j1["track_ids"] + j2["track_ids"]:
        r, raw = call(f"/api/tracks/{tid}/audio")
        print(f"аудио {tid}: {len(raw) / 1024:.0f} КБ, {r.headers.get_content_type()}, Accept-Ranges={r.headers.get('Accept-Ranges')}")
        r2, part = call(f"/api/tracks/{tid}/audio", headers={"Range": "bytes=0-1023"})
        print(f"  Range 0-1023 → HTTP {r2.status}, {len(part)} байт, Content-Range={r2.headers.get('Content-Range')}")
        _, tr = call("/api/tracks?folder=all")
        t = next(x for x in tr["tracks"] if x["id"] == tid)
        print(f"  '{t['title']}' {t['duration']} с, caption: {t['caption']}")
    _, info = call("/api/info")
    print("движок:", info["engine"])


if __name__ == "__main__":
    main()
