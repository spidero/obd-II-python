"""Manufacturer parameters from PID set files: parsing, the formula interpreter, reading and the web/CLI glue."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from obdruid.cli import app as cli_app
from obdruid.core import ConnectionSettings, ObdError, ObdSession, custom_pids
from obdruid.core.custom_pids import Formula, PidSetError, parse_set
from obdruid.core.storage import Storage
from obdruid.web.server import create_app

SET = '''
format = 1
id = "test-car"
name = "Test car"
source = "made up for the tests"

[match]
wmi = ["WF0"]
fuel = "diesel"
years = [2006, 2014]

[[params]]
key = "soot"
label = "Soot load"
label_pl = "Sadza"
request = "22 dd 01"
formula = "(A*256+B)/100"
unit = "%"
decimals = 1
max = 120
warn_high = 80

[[params]]
key = "MISSING"
label = "Not there"
request = "22DDFF"
formula = "A"

[[params]]
key = "NOBODY"
label = "Module that does not exist"
request = "22DE01"
header = "7E2"
formula = "A"

[[params]]
key = "TOO_SHORT"
label = "Formula wants more bytes than the answer has"
request = "22DE01"
header = "7E1"
formula = "A+B"
'''


# --------------------------------------------------------------------------- formulas
@pytest.mark.parametrize("text, data, expected", [
    ("(A*256+B)/100", bytes([0x0D, 0xB6]), 35.1),
    ("A-40", bytes([118]), 78),
    ("signed(A,B)/100", (-35).to_bytes(2, "big", signed=True), -0.35),
    ("signed(A)", bytes([0xFF]), -1),
    ("Signed(A)*2", bytes([0x80]), -256),          # Torque spells it with a capital letter
    ("bit(B,2)", bytes([0, 0x04]), 1),
    ("bit(B,2)", bytes([0, 0xFB]), 0),
    ("(A<<8|B)&0x0FFF", bytes([0xF1, 0x23]), 0x123),
    ("-A%7+max(A,B)", bytes([10, 3]), 14),         # -10 % 7 = 4 in Python
    ("A//2", bytes([7]), 3),
])
def test_formula_values(text, data, expected):
    assert Formula(text)(data) == pytest.approx(expected)


def test_formula_reports_missing_bytes_and_bad_maths_as_no_value():
    assert Formula("A+C").bytes_needed == 3
    assert Formula("A+C")(bytes([1, 2])) is None
    assert Formula("A/B")(bytes([1, 0])) is None
    assert Formula("A<<B")(bytes([1, 200])) is None


@pytest.mark.parametrize("text", [
    "__import__('os').system('true')", "A.real", "open('x')", "A if B else C", "[A]", "A**B", "lambda: A",
    "'text'", "True", "AB + 1", "a + 1", "A; B", "signed(A, x=1)", "eval(A)", "A == B", "~A", "", "A" * 300,
    "(" * 150 + "A" + ")" * 150,
])
def test_formula_rejects_everything_but_arithmetic(text):
    with pytest.raises(PidSetError):
        Formula(text)


# --------------------------------------------------------------------------- files
def test_parse_set():
    found = parse_set(SET)
    assert found.id == "test-car" and [p.name for p in found.params] == ["SOOT", "MISSING", "NOBODY", "TOO_SHORT"]
    soot = found.params[0]
    assert soot.key == "X_TEST_CAR_SOOT" and soot.request == "22DD01" and soot.header == "7E0"
    assert soot.tile == {"max": 120, "warn_high": 80} and soot.status == "unverified"
    assert soot.group == "Test car"                      # the set name is the default group
    assert soot.param().label_pl == "Sadza" and soot.param().source == "test-car"
    assert found.suggested_for("WF0XXXTTFX1234567", 2010, "diesel")
    assert not found.suggested_for("WF0XXXTTFX1234567", 2018, "diesel")
    assert not found.suggested_for("WF0XXXTTFX1234567", 2010, "petrol")
    assert not found.suggested_for(None, None, "diesel")  # the file asks for a Ford VIN


@pytest.mark.parametrize("change, message", [
    (('request = "22DDFF"', 'request = "2EDDFF"'), "Service 2E is not allowed"),     # write
    (('request = "22DDFF"', 'request = "2FDD0103"'), "Service 2F is not allowed"),   # actuator control
    (('request = "22DDFF"', 'request = "3101FF00"'), "Service 31 is not allowed"),   # routine (forced regeneration)
    (('request = "22DDFF"', 'request = "1003"'), "Service 10 is not allowed"),       # session change
    (('request = "22DDFF"', 'request = "04"'), "Invalid request"),
    (('request = "22DDFF"', 'request = "22DD"'), "takes 2 identifier byte"),
    (('header = "7E2"', 'header = "720"'), "Unsupported header"),
    (('formula = "A+B"', 'formula = "__import__(1)"'), "Invalid formula"),
    (('key = "MISSING"', 'key = "SOOT"'), "Duplicate parameter key"),
    (('format = 1', 'format = 2'), "Unsupported file format"),
    (('id = "test-car"', 'id = "../etc"'), "'id' may only contain"),
    (('label = "Not there"', ''), "'label' is required"),
])
def test_parse_set_refuses(change, message):
    with pytest.raises(PidSetError, match=message):
        parse_set(SET.replace(*change))


def test_parse_set_refuses_broken_files():
    for text in ("not toml [", "format = 1\nid = 'x'\nname = 'X'", "x" * (custom_pids.MAX_FILE_SIZE + 1)):
        with pytest.raises(PidSetError):
            parse_set(text)


def test_builtin_sets_are_valid_and_translated(tmp_path):
    sets, errors = custom_pids.load_sets(None)
    assert errors == [] and "simulator" in [s.id for s in sets]
    for s in sets:
        assert s.source, s.id                      # every shipped set says where its data comes from
        for p in s.params:
            assert p.label_pl, f"{s.id}: {p.name} has no Polish label"
    (tmp_path / "broken.toml").write_text("format = 9")
    (tmp_path / "simulator.toml").write_text(SET.replace('id = "test-car"', 'id = "simulator"'))
    sets, errors = custom_pids.load_sets(tmp_path)
    assert [e["file"] for e in errors] == ["broken.toml"]
    assert next(s for s in sets if s.id == "simulator").name == "Test car"   # the user's file wins


# --------------------------------------------------------------------------- session
def test_read_did(session):
    vin = session.read_did("22 f1 90")
    assert vin.ok and vin.data == b"SIM1234567890ABCD" and vin.result == "ok"   # multi frame answer
    refused = session.read_did("22DDFF")
    assert not refused.ok and refused.nrc == 0x31 and refused.result == "nrc31"
    assert session.read_did("22DE01", "7E1").data is not None                   # transmission module
    assert session.read_did("22DE01", "7E2").result == "none"                   # nobody at that address
    for bad in ("2EDD01", "3101FF00", "1003", "04", "22DD"):
        with pytest.raises(ObdError):
            session.read_did(bad)
    with pytest.raises(ObdError):
        session.read_did("22DD01", "720")


def test_standard_queries_still_work_after_addressing_one_module(session):
    seen = session._emulator.commands_seen
    session.read_did("22DE01", "7E1")
    assert seen[-2:] == ["ATSH7E1", "22DE01"]
    start = len(seen)
    assert session.read(["RPM"])[0].value is not None
    assert seen[start] == "ATSH7DF"                       # back to the functional address first
    assert session.read_dtcs().stored and session.status().mil
    assert session.read_monitors().tests and session.read_freeze_frame().present


def test_probe_read_and_cache(session):
    params = parse_set(SET).params
    results = session.probe_custom(params)
    assert results == {"X_TEST_CAR_SOOT": "ok", "X_TEST_CAR_MISSING": "nrc31", "X_TEST_CAR_NOBODY": "none",
                       "X_TEST_CAR_TOO_SHORT": "bad"}
    soot = params[0]
    soot.interval = 60
    session.set_custom_params([soot])
    try:
        assert [p.key for p in session.supported_params()][-1] == "X_TEST_CAR_SOOT"
        seen = session._emulator.commands_seen
        first = session.read(["RPM", "X_TEST_CAR_SOOT", "SPEED"])
        assert [r.key for r in first] == ["RPM", "X_TEST_CAR_SOOT", "SPEED"]
        assert 30 < first[1].value < 70 and first[1].unit == "%" and first[1].pid == "22DD01"
        asked = seen.count("22DD01")
        session.read(["X_TEST_CAR_SOOT"])
        assert seen.count("22DD01") == asked               # within the interval: served from the cache
    finally:
        session.set_custom_params([])
    with pytest.raises(ObdError):
        session.read(["X_TEST_CAR_SOOT"])                   # switched off again


def test_one_request_serves_all_parameters_of_an_identifier(tmp_path):
    storage = Storage(tmp_path)
    with ObdSession(ConnectionSettings(simulate=True, sim_faults=("dpf_regen",))) as s:
        vehicle = storage.select_for_vin(s.vin(), simulated=True)
        results = custom_pids.apply(s, storage, vehicle)
        assert results["X_SIMULATOR_NOT_IN_THIS_CAR"] == "nrc31"
        assert storage.get(vehicle["id"])["pid_probe"] == results           # remembered in the profile
        keys = [p.key for p in s.supported_params() if p.source == "simulator"]
        assert "X_SIMULATOR_NOT_IN_THIS_CAR" not in keys and len(keys) == 8
        seen = s._emulator.commands_seen
        before = len(seen)
        values = {r.key: r.value for r in s.read(keys)}
        assert seen[before:].count("22DD05") == 1                             # two parameters, one request
        assert values["X_SIMULATOR_DPF_REGEN"] == "active" and values["X_SIMULATOR_DPF_REGEN_REQUEST"] == "yes"
        assert values["X_SIMULATOR_DPF_TEMP"] > 550 and values["X_SIMULATOR_INJ_CORRECTION_1"] == -0.35
        assert 70 < values["X_SIMULATOR_GEARBOX_OIL_TEMP"] < 85
        # the second connection does not probe again
        before = len(seen)
        custom_pids.apply(s, storage, storage.get(vehicle["id"]))
        assert "22DDFF" not in seen[before:]


def test_dpf_full_fault():
    with ObdSession(ConnectionSettings(simulate=True, sim_faults=("dpf_full",))) as s:
        soot = next(p for p in custom_pids.load_sets(None)[0][0].params if p.name == "DPF_SOOT")
        assert soot.decode(s.read_did(soot.request).data) == 96.0


# --------------------------------------------------------------------------- web
@pytest.fixture()
def client(tmp_path):
    app = create_app(ConnectionSettings(), connect=False, storage=Storage(tmp_path))
    with TestClient(app) as c:
        yield c


def test_web_sets_for_a_real_car_profile(client):
    data = client.get("/api/pidsets").json()
    assert data["sets"] == [] and data["connected"] is False   # the simulator set is hidden without the simulator
    assert client.post("/api/pidsets", json={"text": "format = 1"}).status_code == 400
    refused = client.post("/api/pidsets", json={"text": SET.replace('"22DDFF"', '"2EDDFF"')})
    assert refused.status_code == 400 and "Service 2E is not allowed" in refused.json()["detail"]

    data = client.post("/api/pidsets", json={"text": SET}).json()
    assert [s["id"] for s in data["sets"]] == ["test-car"]
    assert data["sets"][0]["enabled"] is False and data["sets"][0]["builtin"] is False
    data = client.put("/api/pidsets/enabled", json={"ids": ["test-car", "unknown"]}).json()
    assert data["sets"][0]["enabled"] is True
    assert client.get("/api/vehicles").json()["vehicles"][0]["pidsets"] == ["test-car"]
    assert client.post("/api/pidsets/probe").status_code == 409            # needs a connection
    assert client.delete("/api/pidsets/test-car").json()["sets"] == []
    assert client.delete("/api/pidsets/test-car").status_code == 404


def test_web_simulator_flow(client):
    assert client.post("/api/connect", json={"simulate": True}).status_code == 200
    data = client.get("/api/pidsets").json()
    sim = data["sets"][0]
    assert sim["id"] == "simulator" and sim["enabled"] and sim["locked"] and data["connected"]
    probes = {p["name"]: p["probe"] for p in sim["params"]}
    assert probes["DPF_SOOT"] == "ok" and probes["NOT_IN_THIS_CAR"] == "nrc31"

    params = client.get("/api/params").json()
    soot = next(p for p in params["params"] if p["key"] == "X_SIMULATOR_DPF_SOOT")
    assert soot["label_pl"] == "Zapełnienie DPF sadzą" and soot["status"] == "verified" and soot["pid"] == "22DD01"
    assert params["tiles"]["X_SIMULATOR_DPF_SOOT"]["alarm_high"] == 95
    assert all(p["key"] != "X_SIMULATOR_NOT_IN_THIS_CAR" for p in params["params"])

    read = client.get("/api/read", params={"keys": "RPM,X_SIMULATOR_DPF_SOOT,x_simulator_dpf_regen"}).json()
    assert [r["key"] for r in read["readings"]] == ["RPM", "X_SIMULATOR_DPF_SOOT", "X_SIMULATOR_DPF_REGEN"]
    assert read["readings"][2]["value"] == "idle"

    with client.websocket_connect("/ws/live") as ws:
        ws.send_text(json.dumps({"keys": ["X_SIMULATOR_DPF_TEMP", "SPEED"], "interval": 0.1}))
        assert ws.receive_json()["readings"][0]["unit"] == "°C"

    # a user's set can be added to the simulator too; "check again" probes it
    client.post("/api/pidsets", json={"text": SET})
    data = client.put("/api/pidsets/enabled", json={"ids": ["test-car"]}).json()
    own = next(s for s in data["sets"] if s["id"] == "test-car")
    assert {p["name"]: p["probe"] for p in own["params"]}["SOOT"] == "ok"
    assert client.post("/api/pidsets/probe").status_code == 200
    keys = [p["key"] for p in client.get("/api/params").json()["params"]]
    assert "X_TEST_CAR_SOOT" in keys and "X_TEST_CAR_MISSING" not in keys
    # health check and trouble codes are not disturbed by the extra requests
    assert client.get("/api/dtc").json()["stored"]


# --------------------------------------------------------------------------- CLI
def test_cli_did_and_pids():
    runner = CliRunner()
    r = runner.invoke(cli_app, ["--sim", "did", "22F190"])
    assert r.exit_code == 0 and "SIM1234567890ABCD" in r.output and "17 bytes" in r.output
    r = runner.invoke(cli_app, ["--sim", "did", "22DD01"])
    assert r.exit_code == 0 and "A=" in r.output and "B=" in r.output
    r = runner.invoke(cli_app, ["--sim", "did", "22DDFF"])
    assert r.exit_code == 1 and "7F 22 31" in r.output and "identifier not supported" in r.output
    r = runner.invoke(cli_app, ["--sim", "did", "22DE01", "--header", "7E2"])
    assert r.exit_code == 1 and "no answer" in r.output
    r = runner.invoke(cli_app, ["--sim", "did", "2EDD01"])
    assert r.exit_code == 2 and "not allowed" in r.output
    r = runner.invoke(cli_app, ["--sim", "pids"])
    assert r.exit_code == 0 and "22DD01" in r.output and "simulator" in r.output
    r = runner.invoke(cli_app, ["--sim", "live", "X_SIMULATOR_DPF_SOOT", "-n", "1"])
    assert r.exit_code == 0 and "DPF soot load" in r.output
