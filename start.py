from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "app-startup.log"
HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"

# Embedded Python can have a narrower sys.path than a normal installation.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def server_running(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout):
            return True
    except OSError:
        return False


def open_url() -> bool:
    # os.startfile is more reliable than webbrowser.open on Windows GUI setups.
    try:
        if os.name == "nt":
            os.startfile(URL)  # type: ignore[attr-defined]
            return True
    except Exception as exc:
        log(f"os.startfile não conseguiu abrir o navegador: {exc}")
    try:
        return bool(webbrowser.open(URL, new=2))
    except Exception as exc:
        log(f"webbrowser.open falhou: {exc}")
        return False


def wait_and_open_browser() -> None:
    deadline = time.time() + 45
    while time.time() < deadline:
        if server_running():
            log(f"Servidor respondeu em {URL}. Abrindo o painel...")
            if not open_url():
                log(f"Abra manualmente no navegador: {URL}")
            return
        time.sleep(0.25)
    log(f"O servidor não respondeu em 45s. Veja este log: {LOG}")


def main() -> int:
    # Start each attempt with a visible separator while keeping older diagnostics.
    log("=" * 72)
    log("Kwai Analyzer Portable v2.1 iniciando")
    log(f"Python: {sys.executable}")
    log(f"Versão Python: {sys.version.replace(chr(10), ' ')}")
    log(f"Pasta do app: {ROOT}")

    if server_running():
        log(f"Já existe um servidor em {URL}. Apenas abrindo o painel.")
        open_url()
        return 0

    try:
        log("Importando FastAPI/Uvicorn e o aplicativo...")
        import uvicorn
        from app.main import app
        log("Importação concluída. Iniciando servidor local...")
    except Exception:
        log("ERRO durante a importação do aplicativo:")
        details = traceback.format_exc()
        print(details, flush=True)
        try:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(details + "\n")
        except Exception:
            pass
        return 2

    threading.Thread(target=wait_and_open_browser, daemon=True).start()

    try:
        # Passing the ASGI object directly avoids module re-import/path issues in
        # the Windows embeddable Python distribution.
        uvicorn.run(
            app,
            host=HOST,
            port=PORT,
            reload=False,
            log_level="info",
            access_log=False,
        )
        return 0
    except Exception:
        log("ERRO durante a execução do servidor:")
        details = traceback.format_exc()
        print(details, flush=True)
        try:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(details + "\n")
        except Exception:
            pass
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
