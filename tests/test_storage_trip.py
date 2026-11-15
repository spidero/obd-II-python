import pytest

from obdruid.core import ConnectionSettings, ObdSession
from obdruid.core.storage import Storage, StorageError
from obdruid.core.trip import LiveReader, TripComputer, fuel_flow, fuel_source, trip_params


# --------------------------------------------------------------------------- trip
def test_fuel_flow():
    assert fuel_flow("petrol", fuel_rate=3.2) == 3.2             # the ECU's own value wins
    assert fuel_flow("petrol", maf=10.0) == pytest.approx(3.29, abs=0.01)
    assert fuel_flow("lpg", maf=10.0) > fuel_flow("petrol", maf=10.0)  # LPG is less dense
    assert fuel_flow("diesel", maf=10.0) is None                 # lean burn: MAF says nothing
    assert fuel_flow("petrol") is None


def test_fuel_source():
    assert fuel_source({"MAF", "FUEL_RATE"}, "petrol") == "FUEL_RATE"
    assert fuel_source({"MAF"}, "petrol") == "MAF"
    assert fuel_source({"MAF"}, "diesel") is None
    assert fuel_source({"FUEL_RATE"}, "diesel") == "FUEL_RATE"
    assert [p.key for p in trip_params({"SPEED"}, "diesel")] == ["TRIP_DISTANCE", "TRIP_TIME", "TRIP_AVG_SPEED"]


def test_trip_integration():
    trip = TripComputer("petrol")
    for second in range(0, 3601):          # one hour at 90 km/h and 20 g/s of air
        trip.update(90, maf=20.0, now=float(second))
    assert trip.distance == pytest.approx(90.0)
    assert trip.fuel_used == pytest.approx(6.57, abs=0.01)
    assert trip.average == pytest.approx(7.3, abs=0.05) == pytest.approx(trip.instant, abs=0.05)
    assert trip.summary()["average_speed_kmh"] == 90.0 and trip.idle == 0


def test_trip_standing_and_gaps():
    trip = TripComputer("petrol")
    for second in range(0, 61):
        trip.update(0, maf=2.0, now=float(second))
    assert trip.idle == 60 and trip.distance == 0
    assert trip.instant is None and trip.average is None   # no l/100 km while standing
    assert trip.fuel_used > 0
    used = trip.fuel_used
    trip.update(50, maf=10.0, now=500.0)                   # dashboard was closed for a while: not counted
    assert trip.fuel_used == used and trip.duration == 60
    trip.reset()
    assert trip.distance == 0 and trip.fuel == "petrol" and trip.summary()["distance_km"] == 0


def test_live_reader_adds_calculated_values(session):
    reader = LiveReader(session, TripComputer("petrol"))
    keys = [p.key for p in reader.params()]
    assert "RPM" in keys and keys[-1] == "TRIP_AVG_SPEED"
    first = reader.read(["FUEL_FLOW", "RPM", "TRIP_TIME"])
    assert [r.key for r in first] == ["FUEL_FLOW", "RPM", "TRIP_TIME"]   # order kept, helper PIDs hidden
    assert first[0].unit == "l/h" and 0.2 < first[0].value < 30
    reader.read(["TRIP_TIME"])
    assert reader.trip.duration > 0


# --------------------------------------------------------------------------- storage
def test_vehicle_profiles(tmp_path):
    st = Storage(tmp_path)
    car = st.active()
    assert car["name"] == "My car" and st.active()["id"] == car["id"]   # created once
    assert st.select_for_vin(None)["id"] == car["id"]                    # no VIN: keep the active profile
    assert st.select_for_vin("WVWZZZ1KZAW000001")["id"] == car["id"]     # first VIN is adopted
    other = st.select_for_vin("VF1AAAAAA00000002")
    assert other["id"] != car["id"] and other["name"] == "Car 000002"
    assert st.select_for_vin("WVWZZZ1KZAW000001")["id"] == car["id"]     # known VIN: switch back
    assert st.update(car["id"], fuel="diesel", name="  Golf ")["name"] == "Golf"
    assert st.update(car["id"], fuel="coal")["fuel"] == "petrol"
    st.delete(other["id"])
    assert [v["id"] for v in st.vehicles()] == [car["id"]]
    with pytest.raises(StorageError):
        st.get("nope")


def test_simulator_has_its_own_profile(tmp_path):
    st = Storage(tmp_path)
    car = st.active()
    sim = st.select_for_vin("SIM1234567890ABCD", simulated=True)
    assert sim["name"] == "Simulator" and sim["id"] != car["id"] and st.get(car["id"])["vin"] is None
    assert st.select_for_vin("SIM1234567890ABCD", simulated=True)["id"] == sim["id"]
    assert st.select_for_vin(None)["id"] == car["id"]     # real car again: back to its profile


def test_history_records(tmp_path):
    st = Storage(tmp_path)
    car = st.active()["id"]
    a = st.add_record(car, "health", "90/100 Good", {"score": 90})
    b = st.add_record(car, "health", "50/100 Poor", {"score": 50})
    c = st.add_record(car, "dtc-backup", "Before clearing: P0420", {"dtcs": {}})
    assert a["id"] != b["id"]                               # same second: ids stay unique
    assert [r["id"] for r in st.records(car, "health")] == [b["id"], a["id"]]   # newest first
    assert len(st.records(car)) == 3
    assert st.record(car, c["id"])["data"] == {"dtcs": {}}
    st.delete_record(car, a["id"])
    assert len(st.records(car)) == 2
    for bad in ("../../vehicles", "a/b", "", "x" * 200):
        with pytest.raises(StorageError):
            st.record(car, bad)
    with pytest.raises(StorageError):
        st.add_record(car, "selfie", "", {})


def test_dashboard_layout_per_vehicle(tmp_path):
    st = Storage(tmp_path)
    car = st.active()["id"]
    assert st.dashboard(car) is None
    st.save_dashboard(car, {"keys": ["RPM"], "tiles": {}, "sound": False})
    assert Storage(tmp_path).dashboard(car)["keys"] == ["RPM"]   # survives a restart
    assert st.dashboard(st.create("Other")["id"]) is None


def test_data_dir_migrates_from_obd_tool(tmp_path, monkeypatch):
    from obdruid.core.storage import default_data_dir
    for var in ("OBDRUID_DATA", "OBD_TOOL_DATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "obd-tool").mkdir()
    (tmp_path / "obd-tool" / "supporter.json").write_text("{}")
    assert default_data_dir() == tmp_path / "obdruid"
    assert (tmp_path / "obdruid" / "supporter.json").exists() and not (tmp_path / "obd-tool").exists()
    monkeypatch.setenv("OBD_TOOL_DATA", str(tmp_path / "custom"))  # the old variable still works
    assert default_data_dir() == tmp_path / "custom"


def test_used_profile_does_not_adopt_a_vin(tmp_path):
    st = Storage(tmp_path)
    car = st.active()  # a car without a VIN that already has history
    st.add_record(car["id"], "health", "90/100", {"score": 90})
    new = st.select_for_vin("WF0XXXTTFXDM00001")
    assert new["id"] != car["id"] and new["vin"] == "WF0XXXTTFXDM00001"
    assert not st.get(car["id"]).get("vin")
