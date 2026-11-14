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

    assert client.post("/api/dtc/clear", json={}).status_code == 400
    after = client.post("/api/dtc/clear", json={"confirm": True}).json()
    assert after == {"stored": [], "pending": []}

    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"keys": ["RPM", "COOLANT_TEMP"], "interval": 0.2})
        msg = ws.receive_json()
        assert [r["key"] for r in msg["readings"]] == ["RPM", "COOLANT_TEMP"]

    assert client.post("/api/disconnect").json()["state"] == "disconnected"
