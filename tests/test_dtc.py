import pytest

from obd_tool.core import dtc


@pytest.mark.parametrize("code", ["P0133", "P0420", "C1234", "B0001", "U0100", "P2A00", "P3400"])
def test_roundtrip(code):
    assert dtc.decode(dtc.encode(code)) == code


def test_known_encoding():
    assert dtc.encode("P0133") == bytes([0x01, 0x33])
    assert dtc.encode("U0100") == bytes([0xC1, 0x00])
    assert dtc.decode(b"\x00\x00") is None


def test_invalid():
    with pytest.raises(ValueError):
        dtc.encode("X0133")


def test_system_and_description():
    assert "Ignition" in dtc.system_of("P0301")
    assert "manufacturer" in dtc.system_of("P1234")
    assert "manufacturer" in dtc.system_of("P3000")
    assert "manufacturer" not in dtc.system_of("P3400")
    assert dtc.system_of("U0100").startswith("Network")
    assert dtc.describe("P0420") == "Catalyst System Efficiency Below Threshold"
    assert "OBDResponse" not in dtc.describe("P0133")
