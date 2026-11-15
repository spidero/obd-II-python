"""Desktop GUI: the web UI in a native window (pywebview), browser as a fallback."""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .core import ConnectionSettings

ICON = Path(__file__).parent / "web" / "static" / "logo-192.png"


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


DEFAULT_REMOTE_PORT = 8090


class RemoteError(Exception):
    """The remote OBDruid server cannot be used."""


def normalize_remote(address: str) -> str:
    """'192.168.1.50' -> 'http://192.168.1.50:8090/'; a full URL is kept as is (plus a trailing slash)."""
    address = address.strip()
    if not address:
        raise RemoteError("Empty remote address.")
    bare = "://" not in address  # only a host (and port): use http and the default web port
    if bare:
        address = "http://" + address
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RemoteError(f"Not a valid address: {address}")
    netloc = f"{parts.netloc}:{DEFAULT_REMOTE_PORT}" if bare and not parts.port else parts.netloc
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return f"{parts.scheme}://{netloc}{path}"


def check_remote(url: str, timeout: float = 5.0) -> dict:
    """Make sure an OBDruid web server answers at `url`; returns its /api/state."""
    try:
        with urllib.request.urlopen(url + "api/state", timeout=timeout) as response:
            state = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise RemoteError(f"No OBDruid server at {url} ({e}). Is `obdruid web` running there?") from e
    if not isinstance(state, dict) or "state" not in state:
        raise RemoteError(f"{url} answers, but it is not an OBDruid server.")
    return state


def _open_window(title: str, url: str) -> bool:
    """Show `url` in a native window and block until it is closed. False = no window could be opened."""
    try:
        import webview  # pywebview
    except ImportError:
        print("pywebview is not installed (pip install 'obdruid[gui]'); opening the web browser instead.")
        return False
    try:
        webview.create_window(title, url, width=1280, height=820, min_size=(420, 560))
        try:
            webview.start(icon=str(ICON))  # window/taskbar icon (GTK and Qt)
        except TypeError:  # older pywebview without the icon argument
            webview.start()
    except Exception as e:  # no GUI backend (GTK/Qt) available
        print(f"Could not open a window ({e}); opening the web browser instead.")
        return False
    return True


def run_remote(address: str) -> None:
    """Show the web UI of another machine (e.g. a Pi with the adapter) in the desktop window."""
    url = normalize_remote(address)
    state = check_remote(url)
    host = urlsplit(url).hostname
    print(f"OBDruid on {host}: {state.get('state')}")
    if not _open_window(f"OBDruid – {host}", url):
        webbrowser.open(url)
        print(f"OBDruid: {url}")


def run(settings: ConnectionSettings) -> None:
    server, thread, url = _start_server(settings, _free_port())
    if _open_window("OBDruid", url):
        server.should_exit = True  # window closed -> stop the server
        thread.join(5)
        return

    webbrowser.open(url)
    print(f"OBDruid: {url}  (Ctrl+C to quit)")
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
