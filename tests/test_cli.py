from typer.testing import CliRunner

from obd_tool.cli import app

runner = CliRunner()


def test_info():
    r = runner.invoke(app, ["--sim", "info"])
    assert r.exit_code == 0, r.output
    assert "ELM327 v2.3" in r.output and "SIM1234567890ABCD" in r.output


def test_dtc():
    r = runner.invoke(app, ["--sim", "dtc"])
    assert r.exit_code == 0, r.output
    assert "P0133" in r.output and "P0171" in r.output


def test_clear_needs_confirmation():
    r = runner.invoke(app, ["--sim", "clear"], input="n\n")
    assert r.exit_code == 1
    assert "Cancelled" in r.output


def test_clear_yes():
    r = runner.invoke(app, ["--sim", "clear", "--yes"])
    assert r.exit_code == 0, r.output
    assert "Trouble codes cleared" in r.output


def test_live_csv(tmp_path):
    out = tmp_path / "log.csv"
    r = runner.invoke(app, ["--sim", "live", "RPM", "SPEED", "-n", "3", "-i", "0.1", "--csv", str(out)])
    assert r.exit_code == 0, r.output
    lines = out.read_text().splitlines()
    assert lines[0] == "time,RPM,SPEED" and len(lines) == 4


def test_bad_port():
    r = runner.invoke(app, ["--port", "/dev/does-not-exist", "info"])
    assert r.exit_code == 2
