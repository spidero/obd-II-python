import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from obdruid.cli import app as cli_app
from obdruid.core import ConnectionSettings, ObdSession
from obdruid.core.health import Snapshot, _crossings, evaluate, run_check
from obdruid.core.models import Dtc, DtcReport, ReadinessTest, VehicleStatus
from obdruid.core.report_html import render_html
from obdruid.web.server import create_app


def snapshot(samples=None, counters=None, stored=(), pending=(), mil=False, incomplete=()):
    tests = [ReadinessTest(k, k.title(), True, k not in incomplete) for k in ("MISFIRE", "FUEL", "CATALYST")]
    base = {
        "RPM": [700, 690, 710, 705, 695, 700, 702, 698],
        "SPEED": [0] * 8,
        "COOLANT_TEMP": [90] * 8,
        "SHORT_FUEL_TRIM_1": [1.0] * 8,
        "LONG_FUEL_TRIM_1": [2.0] * 8,
        "CONTROL_MODULE_VOLTAGE": [14.1] * 8,
        "INTAKE_PRESSURE": [30] * 8,
    }
    base.update(samples or {})
    return Snapshot(
        status=VehicleStatus(mil=mil, dtc_count=len(stored), ignition_type="spark", readiness=tests),
        dtcs=DtcReport(stored=[Dtc(c, "desc", "stored") for c in stored],
                       pending=[Dtc(c, "desc", "pending") for c in pending]),
        counters=counters or {"RUN_TIME": 1200, "DISTANCE_SINCE_DTC_CLEAR": 5000, "WARMUPS_SINCE_DTC_CLEAR": 200},
        samples={k: [(i * 0.5, float(v)) for i, v in enumerate(vals)] for k, vals in base.items()},
        duration=4.0,
    )


def by_key(report):
    return {f.key: f for f in report.findings}


def test_healthy_car():
    r = evaluate(snapshot())
    assert r.score == 100 and r.grade == "Good"
    assert all(f.severity in ("ok", "skipped") for f in r.findings)


def test_stored_code_is_critical():
    r = evaluate(snapshot(stored=["P0301"], mil=True))
    f = by_key(r)
    assert f["dtc_P0301"].severity == "critical" and f["mil"].severity == "critical"
    assert r.grade == "Poor" and r.score <= 59
    assert r.findings[0].severity == "critical"  # sorted by severity


@pytest.mark.parametrize("st,lt,severity", [(5, 4, "ok"), (8, 6, "warn"), (12, 10, "critical"), (-12, -10, "critical")])
def test_fuel_trims(st, lt, severity):
    r = evaluate(snapshot({"SHORT_FUEL_TRIM_1": [st] * 8, "LONG_FUEL_TRIM_1": [lt] * 8}))
    assert by_key(r)["fuel_trim_b1"].severity == severity


def test_fuel_trims_skipped_when_cold():
    r = evaluate(snapshot({"COOLANT_TEMP": [40] * 8}, counters={"RUN_TIME": 60}))
    assert by_key(r)["fuel_trim_b1"].severity == "skipped"
    assert by_key(r)["cooling"].severity == "skipped"


@pytest.mark.parametrize("volts,severity", [(14.2, "ok"), (13.2, "warn"), (12.5, "critical"), (15.5, "critical")])
def test_charging(volts, severity):
    r = evaluate(snapshot({"CONTROL_MODULE_VOLTAGE": [volts] * 8}))
    assert by_key(r)["charging"].severity == severity


def test_battery_with_engine_off():
    r = evaluate(snapshot({"RPM": [0] * 8, "CONTROL_MODULE_VOLTAGE": [12.0] * 8}))
    assert by_key(r)["battery"].severity == "warn"
    assert by_key(r)["idle"].severity == "skipped"


def test_thermostat_stuck_open():
    r = evaluate(snapshot({"COOLANT_TEMP": [65] * 8}, counters={"RUN_TIME": 1800}))
    assert by_key(r)["cooling"].title.startswith("Engine does not reach")


def test_overheating():
    assert by_key(evaluate(snapshot({"COOLANT_TEMP": [110] * 8})))["cooling"].severity == "critical"


def test_rough_idle_and_vacuum():
    r = evaluate(snapshot({"RPM": [700, 550, 820, 600, 790, 560, 840, 610], "INTAKE_PRESSURE": [58] * 8}))
    assert by_key(r)["idle_stability"].severity == "critical"
    assert by_key(r)["idle_map"].severity == "warn"


def test_idle_ignores_driving_samples():
    r = evaluate(snapshot({"RPM": [700, 2500, 3000, 2800, 2600, 2400, 2200, 2000], "SPEED": [0, 50, 60, 60, 60, 60, 60, 60]}))
    assert by_key(r)["idle"].severity == "skipped"


def test_recently_cleared():
    r = evaluate(snapshot(incomplete=("CATALYST",), counters={"RUN_TIME": 900, "DISTANCE_SINCE_DTC_CLEAR": 8,
                                                              "WARMUPS_SINCE_DTC_CLEAR": 1}))
    assert by_key(r)["readiness"].severity == "warn"
    assert "cleared recently" in by_key(r)["readiness"].title


def test_o2_and_catalyst():
    up = [0.1, 0.8] * 20
    good = evaluate(snapshot({"O2_B1S1": up, "O2_B1S2": [0.7] * 40}))
    assert by_key(good)["o2_upstream"].severity == "ok"
    assert by_key(good)["catalyst"].severity == "ok"
    bad = evaluate(snapshot({"O2_B1S1": up, "O2_B1S2": up}))
    assert by_key(bad)["catalyst"].severity == "warn"
    stuck = evaluate(snapshot({"O2_B1S1": [0.1] * 40}))
    assert by_key(stuck)["o2_upstream"].title == "Upstream O2 sensor is not switching"


def test_crossings_hysteresis():
    assert _crossings([0.1, 0.8, 0.1, 0.8], 0.3, 0.6) == 3
    assert _crossings([0.4, 0.5, 0.45, 0.55], 0.3, 0.6) == 0  # noise inside the band


def test_html_report():
    html = render_html(evaluate(snapshot(stored=["P0420"], mil=True)))
    assert "<title>Vehicle health report</title>" in html and "P0420" in html and "Poor" in html
    assert "window.print()" in html and "Save PDF" in html and "@media print" in html


# ---------------------------------------------------------------- end to end on the simulator
@pytest.mark.parametrize("fault,key,severity", [
    (None, None, None),
    ("lean", "fuel_trim_b1", "critical"),
    ("charging", "charging", "critical"),
    ("catalyst", "catalyst", "warn"),
])
def test_simulator_faults(fault, key, severity):
    settings = ConnectionSettings(simulate=True, sim_codes=False, sim_faults=(fault,) if fault else ())
    progress = []
    with ObdSession(settings) as s:
        report = run_check(s, duration=4, progress=lambda f, m: progress.append(f))
    assert progress[-1] == 1.0
    if fault is None:
        assert report.grade == "Good", [f.to_dict() for f in report.findings if f.severity != "ok"]
    else:
        assert by_key(report)[key].severity == severity


def test_cli_health_json(tmp_path):
    out = tmp_path / "r.html"
    r = CliRunner().invoke(cli_app, ["--sim", "--sim-fault", "overheat", "health", "-d", "3", "--json",
                                     "--html", str(out)])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["grade"] == "Poor"
    assert any(f["key"] == "cooling" and f["severity"] == "critical" for f in data["findings"])
    assert out.read_text().startswith("<!doctype html>")


def test_cli_unknown_fault():
    r = CliRunner().invoke(cli_app, ["--sim", "--sim-fault", "nope", "info"])
    assert r.exit_code == 2


def test_web_health():
    with TestClient(create_app(ConnectionSettings(simulate=True), connect=False)) as c:
        assert c.get("/api/health/last").status_code == 404
        assert c.post("/api/connect", json={"simulate": True}).status_code == 200
        with c.websocket_connect("/ws/health") as ws:
            ws.send_json({"duration": 5})
            messages = []
            while True:
                msg = ws.receive_json()
                messages.append(msg)
                if "report" in msg or "error" in msg:
                    break
        assert "report" in messages[-1], messages[-1]
        assert any("progress" in m for m in messages)
        assert c.get("/api/health/last").json()["score"] == messages[-1]["report"]["score"]
        html = c.get("/api/health/report.html?download=1")
        assert html.status_code == 200 and "attachment" in html.headers["content-disposition"]
