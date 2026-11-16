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

    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.obd_state = app.state.obd
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
        from .core.storage import default_data_dir

        # keep the browser storage (language, last tab) between runs instead of pywebview's private mode
        options = {"icon": str(ICON), "private_mode": False, "storage_path": str(default_data_dir() / "webview")}
        while True:
            try:
                webview.start(**options)
                break
            except TypeError:  # older pywebview: drop the newest option and try again
                if not options:
                    raise
                options.popitem()
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

    # No native window: the UI runs in the web browser. There may be no terminal to press Ctrl+C in
    # (started from the desktop menu, the AppImage or the Windows exe), so quit when the tab is gone.
    webbrowser.open(url)
    print(f"OBDruid: {url}  (Ctrl+C to quit; quits by itself after the browser tab is closed)")
    started = time.monotonic()
    try:
        while thread.is_alive():
            thread.join(0.5)
            if _abandoned(server.obd_state.last_seen, started, time.monotonic()):
                print("No browser tab is using OBDruid any more - quitting.")
                server.should_exit = True
                thread.join(5)
                break
    except KeyboardInterrupt:
        server.should_exit = True


# The page asks for the state every 3 s; browsers slow timers of hidden tabs down to one per minute.
IDLE_QUIT = 180.0      # s without any request after the UI was used
NEVER_OPENED = 600.0   # s without a single request (the browser did not open)


def _abandoned(last_seen: float | None, started: float, now: float) -> bool:
    if last_seen is None:
        return now - started > NEVER_OPENED
    return now - last_seen > IDLE_QUIT
