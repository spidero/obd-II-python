import json

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


def test_dtc_shows_permanent_codes_and_freeze_frame():
    r = runner.invoke(app, ["--sim", "dtc"])
    assert "Permanent" in r.output and "Freeze frame for P0133" in r.output and "2350" in r.output
    data = json.loads(runner.invoke(app, ["--sim", "dtc", "--json"]).output)
    assert data["freeze_frame"]["dtc"]["code"] == "P0133" and data["mil"] is True


def test_monitor_tests():
    r = runner.invoke(app, ["--sim", "tests"])
    assert r.exit_code == 0, r.output
    assert "Catalyst" in r.output and "FAIL" in r.output and "2 failed, 1 close to the limit" in r.output
    data = json.loads(runner.invoke(app, ["--sim", "--sim-no-codes", "tests", "--json"]).output)
    assert data["counts"]["fail"] == 0 and data["counts"]["pass"] == len(data["tests"])


def test_clear_needs_confirmation():
    r = runner.invoke(app, ["--sim", "clear"], input="n\n")
    assert r.exit_code == 1
    assert "Cancelled" in r.output


def test_clear_yes():
    r = runner.invoke(app, ["--sim", "clear", "--yes"])
    assert r.exit_code == 0, r.output
    assert "Trouble codes cleared" in r.output and "Backup saved" in r.output
    r = runner.invoke(app, ["history", "--kind", "dtc-backup"])
    assert r.exit_code == 0 and "Simulator" in r.output and "P0133" in r.output


def test_live_with_calculated_values():
    r = runner.invoke(app, ["--sim", "live", "RPM", "FUEL_FLOW", "-n", "2", "-i", "0.1"])
    assert r.exit_code == 0, r.output
    assert "Fuel flow (calculated)" in r.output and "l/h" in r.output


def test_live_csv(tmp_path):
    out = tmp_path / "log.csv"
    r = runner.invoke(app, ["--sim", "live", "RPM", "SPEED", "-n", "3", "-i", "0.1", "--csv", str(out)])
    assert r.exit_code == 0, r.output
    lines = out.read_text().splitlines()
    assert lines[0] == "time,RPM,SPEED" and len(lines) == 4


def test_bad_port():
    r = runner.invoke(app, ["--port", "/dev/does-not-exist", "info"])
    assert r.exit_code == 2
