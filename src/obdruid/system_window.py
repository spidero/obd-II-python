"""A window for the web UI from what the system already has (Linux builds without a window toolkit).

Tried in this order (`OBDRUID_WINDOW` = auto | gtk | chromium | browser chooses one):
1. GTK + WebKit2GTK of the system Python (gtk_window.py run with /usr/bin/python3) - a real window;
2. Chrome, Chromium, Edge, Brave or Vivaldi in app mode (`--app=URL`): its own window without tabs and
   address bar, with a separate profile, so the process ends when the window is closed;
3. nothing - the caller opens a tab in the default browser.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HELPER = Path(__file__).with_name("gtk_window.py")
ICON = Path(__file__).parent / "web" / "static" / "logo-192.png"

SYSTEM_PYTHONS = ("/usr/bin/python3", "/bin/python3", "/usr/local/bin/python3")
CHROMIUM_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                     "microsoft-edge-stable", "brave-browser", "brave", "vivaldi", "vivaldi-stable")
MODES = ("auto", "gtk", "chromium", "browser")
QUICK_EXIT = 3.0   # s: a window process that ends this fast did not really show a window

# results of open_window()
CLOSED = "closed"        # a window was shown and the user closed it: the program can quit
ELSEWHERE = "elsewhere"  # a running browser took the window over: watch the requests to know when it is gone
NONE = "none"            # no window: open the default browser


def clean_env(environ: dict | None = None) -> dict:
    """Environment for programs of the system.

    The frozen build points LD_LIBRARY_PATH (and a few Python variables) at its own libraries; a system
    browser or Python started with them would load the wrong libraries and crash.
    """
    env = dict(os.environ if environ is None else environ)
    for key in [k for k in env if k.endswith("_ORIG")]:   # PyInstaller keeps the original values here
        value = env.pop(key)
        if value:
            env[key[:-5]] = value
        else:
            env.pop(key[:-5], None)
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle and env.get("LD_LIBRARY_PATH"):
        kept = [p for p in env["LD_LIBRARY_PATH"].split(os.pathsep) if p and not p.startswith(bundle)]
        if kept:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(kept)
        else:
            env.pop("LD_LIBRARY_PATH")
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_LINUX_PROCESS_NAME"):
        env.pop(key, None)
    return env


def gtk_python(env: dict | None = None, run=subprocess.run) -> str | None:
    """A system Python that can show the GTK window, or None."""
    own = os.path.realpath(sys.executable) if getattr(sys, "frozen", False) else None
    seen = set()
    for candidate in (*SYSTEM_PYTHONS, shutil.which("python3")):
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        real = os.path.realpath(candidate)
        if real in seen or real == own:
            continue
        seen.add(real)
        try:
            result = run([candidate, str(HELPER), "--check"], env=env or clean_env(), capture_output=True,
                         text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and "ok" in result.stdout:
            return candidate
    return None


def _profile_dir(executable: str, data_dir: Path) -> Path:
    """A browser profile only for OBDruid. A snap browser may only write below ~/snap/<name>."""
    real = os.path.realpath(executable)
    if executable.startswith("/snap/") or real.startswith("/snap/"):
        return Path.home() / "snap" / os.path.basename(executable) / "common" / "obdruid-profile"
    return data_dir / "browser-profile"


def chromium_command(url: str, data_dir: Path, which=shutil.which) -> list[str] | None:
    """Command line that shows `url` as an app window of a Chromium based browser, or None."""
    for name in CHROMIUM_BROWSERS:
        executable = which(name)
        if not executable:
            continue
        command = [executable, f"--app={url}", f"--user-data-dir={_profile_dir(executable, data_dir)}",
                   "--no-first-run", "--no-default-browser-check", "--class=OBDruid", "--window-size=1280,820"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            command.append("--no-sandbox")   # Chromium refuses to run as root otherwise (containers)
        return command
    return None


def _wait(process) -> int:
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        raise


def _run_window(command: list[str], env: dict, popen=subprocess.Popen) -> tuple[int | None, float]:
    """Start a window process and wait for it: (exit code or None when it could not start, seconds it ran)."""
    started = time.monotonic()
    try:
        process = popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None, 0.0
    return _wait(process), time.monotonic() - started


def open_window(title: str, url: str, data_dir: Path, mode: str | None = None, popen=subprocess.Popen,
                find_python=gtk_python, which=shutil.which) -> str:
    """Show `url` in a window of the system and block until it is closed. Returns CLOSED, ELSEWHERE or NONE."""
    mode = (mode or os.environ.get("OBDRUID_WINDOW") or "auto").lower()
    if mode not in MODES:
        print(f"OBDRUID_WINDOW={mode} is not one of {', '.join(MODES)}; using auto.")
        mode = "auto"
    if mode == "browser" or not sys.platform.startswith("linux"):
        return NONE
    env = clean_env()
    if mode in ("auto", "gtk"):
        python = find_python(env)
        if python:
            code, ran = _run_window([python, str(HELPER), url, title, str(ICON), str(data_dir / "gtk-window")],
                                    env, popen)
            if code == 0 or (code is not None and ran > QUICK_EXIT):
                return CLOSED
            print("The GTK window could not be opened; trying a browser window.")
    if mode in ("auto", "chromium"):
        command = chromium_command(url, data_dir, which)
        if command:
            code, ran = _run_window(command, env, popen)
            if code is not None and ran > QUICK_EXIT:
                return CLOSED
            if code == 0:
                return ELSEWHERE   # an OBDruid window of this profile was already open and got the URL
            print(f"{os.path.basename(command[0])} did not open a window; using the default browser.")
    return NONE


def open_browser(url: str) -> None:
    """A tab in the default browser - started with a clean environment from the frozen build."""
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False) and shutil.which("xdg-open"):
        try:
            subprocess.Popen(["xdg-open", url], env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    import webbrowser

    webbrowser.open(url)
