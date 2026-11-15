import pytest
from typer.testing import CliRunner

from obd_tool import gui
from obd_tool.cli import app as cli_app
from obd_tool.core import ConnectionSettings


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
        assert opened == [("OBD Tool – 127.0.0.1", url)]
    finally:
        server.should_exit = True
        thread.join(5)


def test_cli_remote_unreachable():
    port = gui._free_port()  # nothing listens there
    r = CliRunner().invoke(cli_app, ["gui", "--remote", f"127.0.0.1:{port}"])
    assert r.exit_code == 1
    assert "No OBD Tool server" in r.output
