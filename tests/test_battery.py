import pytest
from fastapi.testclient import TestClient

from obdruid.core import ConnectionSettings
from obdruid.core.battery import BatteryData, evaluate_battery, run_battery_test, state_of_charge
from obdruid.core.models import Reading
from obdruid.web.server import create_app


def by_key(report):
    return {f.key: f for f in report.findings}


def test_state_of_charge():
    assert state_of_charge(12.7) == 100 and state_of_charge(12.24) == 50 and state_of_charge(11.5) == 0


@pytest.mark.parametrize("rest,severity", [(12.62, "ok"), (12.3, "warn"), (11.9, "critical")])
def test_rest_voltage(rest, severity):
    r = evaluate_battery(BatteryData(rest=[rest] * 3))
    assert by_key(r)["battery_rest"].severity == severity


@pytest.mark.parametrize("dip,severity", [(10.4, "ok"), (9.8, "warn"), (9.1, "critical")])
def test_cranking_dip(dip, severity):
    r = evaluate_battery(BatteryData(rest=[12.6], crank=[12.4, dip, 11.9], started=True))
    assert by_key(r)["battery_crank"].severity == severity


@pytest.mark.parametrize("idle,severity", [(14.2, "ok"), (13.2, "warn"), (12.6, "critical"), (15.4, "critical")])
def test_charging_at_idle(idle, severity):
    r = evaluate_battery(BatteryData(engine_was_running=True, idle=[idle] * 6))
    assert by_key(r)["charging_idle"].severity == severity
    assert by_key(r)["battery_rest"].severity == "skipped"


def test_ripple_and_load():
    r = evaluate_battery(BatteryData(engine_was_running=True, idle=[14.0, 14.7, 13.9, 14.6, 14.0, 14.8],
                                     load=[12.8] * 4, load_rpm=[1200] * 4))
    f = by_key(r)
    assert f["charging_ripple"].severity == "warn"
    assert f["charging_load"].severity == "critical"
    assert f["charging_load_rpm"].severity == "info"
    assert r.kind == "battery" and r.grade == "Poor"


class FakeSession:
    """Engine off, then the user starts it: voltage dips while cranking and rises once it runs."""

    def __init__(self, lose_power=False):
        self.t = 0
        self.running = False
        self.lose_power = lose_power

    def adapter_info(self):
        raise RuntimeError("not needed")

    def adapter_voltage(self):
        self.t += 1
        if self.cranking:
            if self.lose_power and self.t > self.crank_start + 2:
                raise RuntimeError("adapter lost power")
            if self.t > self.crank_start + 8:
                self.running = True
            return 9.9 if self.t == self.crank_start + 3 else 11.2
        return 14.2 if self.running else 12.55

    cranking = False
    crank_start = 0

    def read(self, keys):
        return [Reading(key="RPM", label="RPM", value=780.0 if self.running else 0.0, unit="rpm")]


def test_guided_run_from_engine_off():
    s = FakeSession()
    asked = []

    def ask(step, prompt):
        asked.append(step)
        if step == "crank":
            s.cranking, s.crank_start = True, s.t
        if step == "idle":
            s.cranking = False
        return step != "load"  # the user skips the load step

    report = run_battery_test(s, ask, rest_s=0.05, crank_timeout_s=5, idle_s=0.05, load_s=0.05)
    f = by_key(report)
    assert asked == ["rest", "crank", "idle", "load"]
    assert f["battery_rest"].severity == "ok"
    assert f["battery_crank"].severity == "warn" and f["battery_crank"].value == "9.90 V"
    assert f["charging_idle"].severity == "ok"
    assert f["charging_load"].severity == "skipped"


def test_adapter_losing_power_while_cranking():
    s = FakeSession(lose_power=True)

    def ask(step, prompt):
        if step == "crank":
            s.cranking, s.crank_start = True, s.t
        return True

    report = run_battery_test(s, ask, rest_s=0.05, crank_timeout_s=5)
    assert by_key(report)["battery_crank"].title == "Adapter dropped out while starting"


def test_web_battery_test(tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    with TestClient(create_app(ConnectionSettings(simulate=True), connect=False)) as c:
        assert c.post("/api/connect", json={"simulate": True}).status_code == 200
        with c.websocket_connect("/ws/battery") as ws:
            ws.send_json({"start": True})
            asked, report = [], None
            while report is None:
                msg = ws.receive_json()
                assert "error" not in msg, msg
                if "ask" in msg:
                    asked.append(msg["ask"])
                    ws.send_json({"go": True} if msg["ask"] == "idle" else {"skip": True})
                report = msg.get("report")
        assert asked == ["idle", "load"]  # the simulated engine is already running
        assert report["kind"] == "battery" and "charging_idle" in {f["key"] for f in report["findings"]}
        html = c.get(f"/api/history/{report['record']['id']}/report.html?lang=pl")
        assert html.status_code == 200 and "Test akumulatora i ładowania" in html.text
        kinds = {r["kind"] for r in c.get("/api/history").json()["records"]}
        assert "battery" in kinds
