"""Desktop GUI: the web UI in a native window (pywebview), browser as a fallback."""

from __future__ import annotations

import socket
import threading
import time
import urllib.request
import webbrowser

from .core import ConnectionSettings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(settings: ConnectionSettings, port: int):
    import uvicorn

    from .web.server import create_app

    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="obd-web", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(100):  # wait up to 10 s for the server
        try:
            urllib.request.urlopen(url + "api/state", timeout=0.5)
            break
        except OSError:
            time.sleep(0.1)
    return server, thread, url


def run(settings: ConnectionSettings) -> None:
    server, thread, url = _start_server(settings, _free_port())
    try:
        import webview  # pywebview
    except ImportError:
        webview = None

    if webview is not None:
        try:
            webview.create_window("OBD Tool", url, width=1280, height=820, min_size=(420, 560))
            webview.start()
        except Exception as e:  # no GUI backend (GTK/Qt) available
            print(f"Could not open a window ({e}); opening the web browser instead.")
        else:
            server.should_exit = True  # window closed -> stop the server
            thread.join(5)
            return
    else:
        print("pywebview is not installed (pip install 'obd-tool[gui]'); opening the web browser instead.")

    webbrowser.open(url)
    print(f"OBD Tool: {url}  (Ctrl+C to quit)")
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
