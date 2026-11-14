from obd_tool.core import DEFAULT_DASHBOARD


def test_adapter_info(session):
    info = session.adapter_info()
    assert info.elm_version == "ELM327 v2.3"
    assert info.protocol_id == "6"
    assert 13 < info.voltage < 15
    assert info.simulated


def test_vin(session):
    assert session.vin() == "SIM1234567890ABCD"


def test_supported_params(session):
    keys = [p.key for p in session.supported_params()]
    assert "RPM" in keys and "COOLANT_TEMP" in keys
    assert "STATUS" not in keys and "PIDS_A" not in keys
    assert all(k in keys for k in DEFAULT_DASHBOARD)


def test_read_values(session):
    readings = {r.key: r for r in session.read(["RPM", "SPEED", "COOLANT_TEMP", "O2_B1S1", "OBD_COMPLIANCE"])}
    assert 500 < readings["RPM"].value < 7000
    assert readings["RPM"].unit == "rpm"
    assert readings["COOLANT_TEMP"].unit == "°C"
    assert 0 < readings["O2_B1S1"].value < 1.3
    assert readings["OBD_COMPLIANCE"].value == "EOBD (Europe)"


def test_unsupported_param_returns_none(session):
    (r,) = session.read(["OIL_TEMP"])
    assert r.value is None


def test_dtc_read_and_clear(session):
    status = session.status()
    assert status.mil and status.dtc_count == 2
    report = session.read_dtcs()
    assert [d.code for d in report.stored] == ["P0133", "P0420"]
    assert [d.code for d in report.pending] == ["P0171"]
    assert report.stored[1].description == "Catalyst System Efficiency Below Threshold"

    session.clear_dtcs()
    assert session.read_dtcs().total == 0
    status = session.status()
    assert not status.mil
    assert any(not t.complete for t in status.readiness)  # monitors reset after clearing
