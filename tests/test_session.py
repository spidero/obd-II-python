from obdruid.core import DEFAULT_DASHBOARD


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


def test_monitor_tests(session):
    report = session.read_monitors()
    assert report.supported
    tests = {t.id: t for t in report.tests}
    slow = tests["01:05"]  # upstream O2 sensor switch time, limit 150 ms (the simulator has P0133 stored)
    assert slow.monitor == "O2 sensor B1S1" and slow.unit == "ms"
    assert (slow.value, slow.min, slow.max, slow.status) == (168, None, 150, "fail")
    assert tests["01:06"].status == "near" and tests["01:06"].margin == 6.0
    assert tests["21:80"].status == "fail" and tests["21:80"].monitor == "Catalyst bank 1"
    heater = tests["41:82"]  # two sided limit
    assert (heater.min, heater.max, heater.status) == (0.4, 2.0, "pass")
    assert tests["A2:0C"].monitor == "Misfire cylinder 1" and tests["A2:0C"].max is None
    assert report.count("fail") == 2 and report.count("near") == 1


def test_dtc_read_and_clear(session):
    status = session.status()
    assert status.mil and status.dtc_count == 2
    report = session.read_dtcs()
    assert [d.code for d in report.stored] == ["P0133", "P0420"]
    assert [d.code for d in report.pending] == ["P0171"]
    assert report.stored[1].description == "Catalyst System Efficiency Below Threshold"

    assert [d.code for d in report.permanent] == ["P0420"] and report.permanent_supported

    frame = session.read_freeze_frame()
    assert frame.supported and frame.dtc.code == "P0133"
    values = {r.key: r.value for r in frame.readings}
    assert values["RPM"] == 2350 and values["SPEED"] == 78 and values["COOLANT_TEMP"] == 88
    assert all(r.pid.startswith("02") for r in frame.readings)

    session.clear_dtcs()
    after = session.read_dtcs()
    assert after.total == 0
    assert [d.code for d in after.permanent] == ["P0420"]  # permanent codes cannot be cleared
    frame = session.read_freeze_frame()
    assert frame.supported and not frame.present and frame.readings == []
    assert session.read_monitors().tests == []  # test results are reset by clearing
    status = session.status()
    assert not status.mil
    assert any(not t.complete for t in status.readiness)  # monitors reset after clearing


def test_fingerprint_of_the_simulator(session):
    from obdruid.core.vehicle_id import same_car

    vin, none = session.identify()
    assert vin and none is None                       # with a VIN no fingerprint is worked out
    first, second = session.fingerprint(), session.fingerprint()
    assert first["protocol"] == "6" and first["ecus"] and int(first["pids"], 16)
    assert same_car(first, second) is True
