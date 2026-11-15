import pytest
from fastapi.testclient import TestClient

from obd_tool.core import ConnectionSettings
from obd_tool.web.server import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app(ConnectionSettings(), connect=False)
    with TestClient(app) as c:
        yield c


def test_static_index(client):
    r = client.get("/")
    assert r.status_code == 200 and "OBD Tool" in r.text
    assert client.get("/app.js").status_code == 200


def test_requires_connection(client):
    assert client.get("/api/state").json()["state"] == "disconnected"
    assert client.get("/api/dtc").status_code == 409


def test_full_flow(client):
    r = client.post("/api/connect", json={"simulate": True})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "connected"

    info = client.get("/api/info").json()
    assert info["adapter"]["simulated"] and info["vin"] == "SIM1234567890ABCD"

    params = client.get("/api/params").json()
    assert "RPM" in params["default"]

    read = client.get("/api/read", params={"keys": "RPM,SPEED"}).json()
    assert [r["key"] for r in read["readings"]] == ["RPM", "SPEED"]

    dtc = client.get("/api/dtc").json()
    assert len(dtc["stored"]) == 2 and dtc["status"]["mil"] is True

    assert dtc["permanent"][0]["code"] == "P0420" and dtc["permanent_supported"]
    assert dtc["freeze_frame"]["dtc"]["code"] == "P0133"
    assert client.get("/api/freeze-frame").json()["present"] is True

    monitors = client.get("/api/monitors").json()
    assert monitors["supported"] and monitors["counts"]["fail"] == 2

    assert client.post("/api/dtc/clear", json={}).status_code == 400
    after = client.post("/api/dtc/clear", json={"confirm": True}).json()
    assert after["stored"] == [] and after["pending"] == []
    assert [d["code"] for d in after["permanent"]] == ["P0420"]  # permanent codes survive clearing
    # the codes and the freeze frame were saved to the history before clearing
    assert after["backup"]["kind"] == "dtc-backup"
    backup = client.get(f"/api/history/{after['backup']['id']}").json()
    assert [d["code"] for d in backup["data"]["dtcs"]["stored"]] == ["P0133", "P0420"]
    assert backup["data"]["freeze_frame"]["dtc"]["code"] == "P0133"
    assert client.get("/api/freeze-frame").json()["present"] is False

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"keys": ["RPM", "COOLANT_TEMP"], "interval": 0.2})
        msg = ws.receive_json()
        assert [r["key"] for r in msg["readings"]] == ["RPM", "COOLANT_TEMP"]

    assert client.post("/api/disconnect").json()["state"] == "disconnected"


def test_dashboard_trip_and_vehicles(client):
    assert client.post("/api/connect", json={"simulate": True}).status_code == 200
    state = client.get("/api/state").json()
    assert state["vehicle"]["name"] == "Simulator"  # the simulator never lands in a real car's profile

    params = client.get("/api/params").json()
    keys = {p["key"] for p in params["params"]}
    assert {"FUEL_FLOW", "FUEL_ECONOMY", "TRIP_DISTANCE"} <= keys
    assert params["tiles"]["COOLANT_TEMP"]["alarm_high"] == 110

    read = client.get("/api/read", params={"keys": "RPM,FUEL_FLOW,TRIP_TIME"}).json()["readings"]
    assert [r["key"] for r in read] == ["RPM", "FUEL_FLOW", "TRIP_TIME"]
    assert 0.2 < read[1]["value"] < 30 and read[1]["unit"] == "l/h"

    # dashboard layout is stored per vehicle
    assert client.get("/api/dashboard").json()["layout"] is None
    layout = {"keys": ["rpm", "SPEED"], "tiles": {"RPM": {"type": "gauge", "max": 6000, "alarm_high": 5500}},
              "sound": True}
    saved = client.put("/api/dashboard", json=layout).json()["layout"]
    assert saved["keys"] == ["RPM", "SPEED"] and saved["tiles"]["RPM"]["type"] == "gauge"
    assert client.get("/api/dashboard").json()["layout"] == saved
    assert client.put("/api/dashboard", json={"keys": [], "tiles": {"RPM": {"type": "pie"}}}).status_code == 400

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"keys": ["TRIP_DISTANCE"], "interval": 0.2})
        msg = ws.receive_json()
        assert msg["readings"][0]["key"] == "TRIP_DISTANCE" and "distance_km" in msg["trip"]

    assert client.post("/api/trip/reset", json={"save": True}).json()["saved"] is None  # too short to keep

    # vehicle profiles
    car = client.post("/api/vehicles", json={"name": "Diesel van", "fuel": "diesel"}).json()
    assert client.get("/api/vehicles").json()["active"] == car["id"]
    assert client.get("/api/dashboard").json()["layout"] is None  # a new car starts with its own layout
    assert client.post("/api/vehicles", json={"name": "x", "fuel": "coal"}).status_code == 400
    updated = client.put(f"/api/vehicles/{car['id']}", json={"notes": "blue"}).json()
    assert updated["notes"] == "blue" and updated["name"] == "Diesel van"
    assert client.delete(f"/api/vehicles/{car['id']}").status_code == 200
    assert client.delete("/api/vehicles/nope").status_code == 404
    assert client.get("/api/history/..%2F..%2Fvehicles").status_code == 404

    about = client.get("/api/about").json()
    assert about["adapters"][0]["status"] == "tested" and about["repo"].startswith("https://")
    assert client.post("/api/disconnect").json()["state"] == "disconnected"


def test_health_report_is_saved_to_history(client):
    assert client.post("/api/connect", json={"simulate": True}).status_code == 200
    with client.websocket_connect("/ws/health") as ws:
        ws.send_json({"duration": 5})
        while "report" not in (msg := ws.receive_json()):
            assert "error" not in msg, msg
    record = msg["report"]["record"]
    assert record["kind"] == "health"
    listed = client.get("/api/history", params={"kind": "health"}).json()["records"]
    assert listed[0]["id"] == record["id"]
    html = client.get(f"/api/history/{record['id']}/report.html")
    assert html.status_code == 200 and "Vehicle health report" in html.text
    assert client.delete(f"/api/history/{record['id']}").status_code == 200
    assert client.get(f"/api/history/{record['id']}").status_code == 404
    client.post("/api/disconnect")


def test_static_files_are_revalidated():
    with TestClient(create_app(ConnectionSettings(simulate=True), connect=False)) as c:
        for path in ("/", "/style.css", "/app.js"):
            r = c.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
