import pytest
from typer.testing import CliRunner

from obdruid import gui
from obdruid.cli import app as cli_app
from obdruid.core import ConnectionSettings


@pytest.mark.parametrize("address,url", [
    ("192.168.1.50", "http://192.168.1.50:8090/"),
    ("raspberrypi.local:9000", "http://raspberrypi.local:9000/"),
    ("http://pi:8090", "http://pi:8090/"),
    ("http://pi", "http://pi/"),                                   # full URL: no default port added
    ("https://obd.example.com/tool", "https://obd.example.com/tool/"),
    (" http://[fd00::6]:8090/ ", "http://[fd00::6]:8090/"),
])
def test_normalize_remote(address, url):
    assert gui.normalize_remote(address) == url


@pytest.mark.parametrize("address", ["", "ftp://host", "http://"])
def test_normalize_remote_rejects(address):
    with pytest.raises(gui.RemoteError):
        gui.normalize_remote(address)


def test_check_remote_and_window(monkeypatch):
    server, thread, url = gui._start_server(ConnectionSettings(simulate=True), gui._free_port())
    try:
        assert gui.check_remote(url)["state"] in ("connected", "connecting", "disconnected", "error")
        opened = []
        monkeypatch.setattr(gui, "_open_window", lambda title, u: opened.append((title, u)) or True)
        gui.run_remote(url)
        assert opened == [("OBDruid – 127.0.0.1", url)]
    finally:
        server.should_exit = True
        thread.join(5)


def test_cli_remote_unreachable():
    port = gui._free_port()  # nothing listens there
    r = CliRunner().invoke(cli_app, ["gui", "--remote", f"127.0.0.1:{port}"])
    assert r.exit_code == 1
    assert "No OBDruid server" in r.output


# ---------------------------------------------------------------- desktop builds (no terminal, UI in the browser)
def test_browser_mode_quits_when_the_tab_is_gone():
    assert not gui._abandoned(None, started=0, now=gui.NEVER_OPENED - 1)      # still waiting for the browser
    assert gui._abandoned(None, started=0, now=gui.NEVER_OPENED + 1)          # it never came
    assert not gui._abandoned(100.0, started=0, now=100 + gui.IDLE_QUIT - 1)  # a hidden tab polls once a minute
    assert gui._abandoned(100.0, started=0, now=100 + gui.IDLE_QUIT + 1)


def test_server_notes_the_last_request(tmp_path):
    from fastapi.testclient import TestClient

    from obdruid.core import ConnectionSettings
    from obdruid.core.storage import Storage
    from obdruid.web.server import create_app

    app = create_app(ConnectionSettings(), connect=False, storage=Storage(tmp_path))
    with TestClient(app) as client:
        assert app.state.obd.last_seen is None
        client.get("/api/state")
        assert app.state.obd.last_seen is not None


def test_desktop_entry_runs_commands_and_survives_without_a_console(monkeypatch, capsys, tmp_path):
    import sys

    from obdruid import desktop

    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["OBDruid", "--version"])
    with pytest.raises(SystemExit) as stop:
        desktop.main()
    assert stop.value.code == 0 and "obdruid" in capsys.readouterr().out.lower()
    # a windowed Windows program has no stdout/stderr at all
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop._log_when_there_is_no_console()
    print("hello log")
    sys.stdout.flush()
    assert "hello log" in (tmp_path / "obdruid.log").read_text(encoding="utf-8")
