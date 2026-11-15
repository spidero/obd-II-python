import pytest

from obdruid.core import ConnectionSettings, ObdSession
from obdruid.core.diag import monitor_name, parse_monitor, rate, read_freeze_frame, read_monitors, read_permanent


@pytest.mark.parametrize("value,lo,hi,status,margin", [
    (62, None, 150, "pass", 58.7),      # upper limit only
    (141, None, 150, "near", 6.0),
    (168, None, 150, "fail", 0.0),
    (0.782, 0.6, None, "pass", 30.3),   # lower limit only
    (0.61, 0.6, None, "near", 1.7),
    (0.5, 0.6, None, "fail", 0.0),
    (1.02, 0.4, 2.0, "pass", 38.7),     # window: distance to the nearest edge
    (1.92, 0.4, 2.0, "near", 5.0),
    (5, None, None, "pass", None),      # no limits (misfire counters)
    (649.772, 649.772, 1274.534, "pass", None),  # value equals a limit: ECU echoes its threshold (real car, 02:81)
    (150, None, 150, "pass", None),
])
def test_rate(value, lo, hi, status, margin):
    assert rate(value, lo, hi) == (status, margin)


def test_parse_monitor_skips_tests_that_did_not_run():
    ran = bytes([0x21, 0x80, 0x20]) + (64).to_bytes(2, "big") + bytes(2) + (128).to_bytes(2, "big")
    not_run = bytes([0x21, 0x81, 0x20]) + bytes(6)
    unknown_unit = bytes([0x21, 0x82, 0xEE]) + bytes([0, 1, 0, 0, 0, 9])
    (test,) = parse_monitor(bytes([0x46]) + ran + not_run + unknown_unit)
    assert (test.mid, test.tid, test.value, test.min, test.max) == (0x21, 0x80, 0.25, None, 0.5)
    assert test.margin == 50.0


def test_signed_units():
    # unit 0x96: signed, 0.1 °C per bit
    block = bytes([0x31, 0x90, 0x96]) + (-50).to_bytes(2, "big", signed=True) + b"\x80\x00" + (100).to_bytes(2, "big")
    (test,) = parse_monitor(bytes([0x46]) + block)
    assert (test.value, test.min, test.max, test.unit) == (-5.0, None, 10.0, "°C")


def test_monitor_names():
    assert monitor_name(0x01) == "O2 sensor B1S1" and monitor_name(0x06) == "O2 sensor B2S2"
    assert monitor_name(0xA5) == "Misfire cylinder 4" and monitor_name(0xEE) == "Monitor $EE"


def test_car_without_the_optional_services():
    """An ECU that answers NO DATA (or a negative response) to modes 02, 06 and 0A."""
    def no_data(command):
        return []

    def negative(command):
        return [bytes([0x7F, int(command[:2], 16), 0x11])]

    for query in (no_data, negative):
        assert read_permanent(query) is None
        assert read_freeze_frame(query, lambda v: (v, "")).supported is False
        assert read_monitors(query).supported is False


def test_healthy_simulator_passes_all_tests():
    s = ObdSession(ConnectionSettings(simulate=True, sim_codes=False))
    s.connect()
    try:
        report = s.read_monitors()
        assert report.tests and report.count("fail") == 0 and report.count("near") == 0
        assert not s.read_freeze_frame().present
        assert s.read_dtcs().permanent == []
    finally:
        s.close()
