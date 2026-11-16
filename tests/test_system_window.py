"""Window of the system for builds without a window toolkit (AppImage): environment, choice, fallbacks."""

import subprocess
import sys
from pathlib import Path

import pytest

from obdruid import system_window as sw


def test_clean_env_undoes_what_the_frozen_build_changed(monkeypatch):
    monkeypatch.setattr(sw.sys, "_MEIPASS", "/tmp/.mount_OBDxyz/usr/lib/obdruid/_internal", raising=False)
    env = sw.clean_env({"LD_LIBRARY_PATH": "/tmp/.mount_OBDxyz/usr/lib/obdruid/_internal", "LD_LIBRARY_PATH_ORIG": "/opt/lib",
                        "PYTHONHOME": "/x", "_MEIPASS2": "/y", "HOME": "/home/u", "DISPLAY": ":0"})
    assert env == {"LD_LIBRARY_PATH": "/opt/lib", "HOME": "/home/u", "DISPLAY": ":0"}
    env = sw.clean_env({"LD_LIBRARY_PATH": "/tmp/.mount_OBDxyz/usr/lib/obdruid/_internal:/usr/local/lib"})
    assert env == {"LD_LIBRARY_PATH": "/usr/local/lib"}
    assert "LD_LIBRARY_PATH" not in sw.clean_env({"LD_LIBRARY_PATH": "/tmp/.mount_OBDxyz/usr/lib/obdruid/_internal"})
    assert "LD_LIBRARY_PATH" not in sw.clean_env({"LD_LIBRARY_PATH": "/b", "LD_LIBRARY_PATH_ORIG": ""})


def test_chromium_app_command(monkeypatch, tmp_path):
    found = {"chromium": "/usr/bin/chromium", "brave-browser": "/usr/bin/brave-browser"}
    monkeypatch.setattr(sw.os, "geteuid", lambda: 1000, raising=False)
    command = sw.chromium_command("http://127.0.0.1:5000/", tmp_path, which=found.get)
    assert command[0] == "/usr/bin/chromium" and "--app=http://127.0.0.1:5000/" in command
    assert f"--user-data-dir={tmp_path / 'browser-profile'}" in command and "--no-sandbox" not in command
    snap = sw.chromium_command("http://x/", tmp_path, which={"chromium": "/snap/bin/chromium"}.get)
    assert f"--user-data-dir={Path.home() / 'snap' / 'chromium' / 'common' / 'obdruid-profile'}" in snap
    monkeypatch.setattr(sw.os, "geteuid", lambda: 0, raising=False)
    assert "--no-sandbox" in sw.chromium_command("http://x/", tmp_path, which=found.get)
    assert sw.chromium_command("http://x/", tmp_path, which=lambda name: None) is None


class FakeProcess:
    def __init__(self, code, seconds, log, command):
        self.code, self.seconds = code, seconds
        log.append(command)

    def wait(self):
        sw.time.monotonic.advance(self.seconds)
        return self.code


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def fake_popen(results, log):
    """results: list of (exit code, seconds the window stayed open); OSError to fail the start."""
    def popen(command, **kwargs):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeProcess(*result, log, command)
    return popen


@pytest.fixture()
def linux(monkeypatch):
    monkeypatch.setattr(sw.sys, "platform", "linux")
    monkeypatch.delenv("OBDRUID_WINDOW", raising=False)
    monkeypatch.setattr(sw.time, "monotonic", Clock())


def open_window(tmp_path, results, python="/usr/bin/python3", browsers=("chromium",), mode=None):
    log = []
    found = {name: f"/usr/bin/{name}" for name in browsers}
    result = sw.open_window("OBDruid", "http://127.0.0.1:5000/", tmp_path, mode=mode, popen=fake_popen(results, log),
                            find_python=lambda env: python, which=found.get)
    return result, [Path(c[0]).name for c in log]


def test_gtk_window_first(linux, tmp_path):
    assert open_window(tmp_path, [(0, 600)]) == (sw.CLOSED, ["python3"])


def test_gtk_failure_falls_back_to_a_chromium_window(linux, tmp_path):
    assert open_window(tmp_path, [(1, 0.5), (0, 900)]) == (sw.CLOSED, ["python3", "chromium"])


def test_chromium_handing_over_to_a_running_window(linux, tmp_path):
    assert open_window(tmp_path, [(0, 0.4)], python=None) == (sw.ELSEWHERE, ["chromium"])


def test_nothing_works_means_the_default_browser(linux, tmp_path):
    assert open_window(tmp_path, [(1, 0.5), (1, 0.5)]) == (sw.NONE, ["python3", "chromium"])
    assert open_window(tmp_path, [OSError("gone")], python=None) == (sw.NONE, [])
    assert open_window(tmp_path, [], python=None, browsers=()) == (sw.NONE, [])


def test_mode_choice(linux, tmp_path, monkeypatch):
    assert open_window(tmp_path, [(0, 600)], mode="chromium") == (sw.CLOSED, ["chromium"])
    assert open_window(tmp_path, [(1, 1)], mode="gtk") == (sw.NONE, ["python3"])
    assert open_window(tmp_path, [], mode="browser") == (sw.NONE, [])
    monkeypatch.setenv("OBDRUID_WINDOW", "browser")
    assert open_window(tmp_path, []) == (sw.NONE, [])
    monkeypatch.setattr(sw.sys, "platform", "win32")   # Windows has its own window (pywebview + WebView2)
    monkeypatch.delenv("OBDRUID_WINDOW")
    assert open_window(tmp_path, []) == (sw.NONE, [])


def test_helper_check_without_gtk_is_a_clean_no():
    """The helper runs with any Python; without GTK it must say so with its exit code, not a traceback."""
    result = subprocess.run([sys.executable, str(sw.HELPER), "--check"], capture_output=True, text=True, timeout=60)
    try:
        import gi  # noqa: F401
        has_gtk = True
    except ImportError:
        has_gtk = False
    if has_gtk:
        assert result.returncode in (0, 3)
    else:
        assert result.returncode == 3 and "Traceback" not in result.stderr


def test_gtk_python_skips_pythons_without_gtk(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command[0])
        return subprocess.CompletedProcess(command, 0 if command[0] == "/usr/bin/python3" else 3, stdout="ok\n")

    monkeypatch.setattr(sw, "SYSTEM_PYTHONS", ("/bin/python3", "/usr/bin/python3"))
    monkeypatch.setattr(sw.os, "access", lambda path, mode: True)
    monkeypatch.setattr(sw.os.path, "realpath", lambda path: path)
    monkeypatch.setattr(sw.shutil, "which", lambda name: None)
    assert sw.gtk_python({}, run=run) == "/usr/bin/python3" and calls == ["/bin/python3", "/usr/bin/python3"]
