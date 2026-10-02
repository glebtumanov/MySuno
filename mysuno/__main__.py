"""Запуск: python -m mysuno  (сервер на http://127.0.0.1:8000)."""
import os
import threading
import webbrowser

import uvicorn


def main() -> None:
    host = os.environ.get("MYSUNO_HOST", "127.0.0.1")
    port = int(os.environ.get("MYSUNO_PORT", "8000"))
    if os.environ.get("MYSUNO_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run("mysuno.server:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
