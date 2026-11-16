"""The OBDb converter (tools/obdb_convert.py): its formulas must give what the OBDb definition says."""

import importlib.util
import json
import random
from pathlib import Path

import pytest

from obdruid.core import custom_pids

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("obdb_convert", ROOT / "tools" / "obdb_convert.py")
conv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conv)

FORMATS = [
    {"len": 8},
    {"len": 16, "mul": 0.0871944108, "add": -57.142857},
    {"len": 24, "mul": 6, "div": 25},
    {"bix": 56, "len": 16, "blsb": True, "div": 4},
    {"bix": 272, "len": 8, "mul": 0.75, "add": -48},                  # byte 34: letter AI
    {"bix": 16, "len": 16, "sign": True, "div": 10},
    {"bix": 24, "len": 16, "blsb": True, "sign": True, "mul": 10, "div": 100},
    {"bix": 170, "len": 1},                                           # a single bit
    {"bix": 3, "len": 10, "mul": 0.5},                                # a field across two bytes
    {"bix": 12, "len": 6, "sign": True, "add": 2},                    # a signed field
    {"bix": 8, "len": 32, "div": 1000},
]


@pytest.mark.parametrize("fmt", FORMATS)
def test_formula_matches_obdb_definition(fmt):
    formula = custom_pids.Formula(conv.formula(fmt))
    rng = random.Random(7)
    for _ in range(200):
        data = bytes(rng.randrange(256) for _ in range(40))
        assert formula(data) == pytest.approx(conv.reference_value(fmt, data))


def test_reference_decoder_reads_known_answers():
    assert conv.reference_value({"len": 16, "div": 10}, bytes([0x01, 0x02])) == pytest.approx(25.8)
    assert conv.reference_value({"bix": 8, "len": 16, "blsb": True}, bytes([9, 0x34, 0x12])) == 0x1234
    assert conv.reference_value({"bix": 2, "len": 1}, bytes([0b00100000])) == 1
    assert conv.reference_value({"len": 8, "sign": True}, bytes([0xFF])) == -1
    assert conv.reference_value({"len": 16}, bytes([1])) is None                   # answer too short


def test_unit_conversion_is_folded_into_the_formula():
    fahrenheit = {"len": 8, "add": -40, "unit": "fahrenheit"}
    target, factor, offset = conv.UNITS["fahrenheit"]
    formula = custom_pids.Formula(conv.formula(fahrenheit, factor, offset))
    assert target == "°C" and formula(bytes([252])) == pytest.approx(100)       # 252 - 40 = 212 °F
    miles = {"len": 16, "unit": "miles"}
    assert custom_pids.Formula(conv.formula(miles, *conv.UNITS["miles"][1:]))(bytes([0, 100])) == pytest.approx(160.9344)


def test_too_long_or_unsupported_fields_are_skipped():
    with pytest.raises(conv.Skip):
        conv.raw_expression({"bix": 3, "len": 16, "blsb": True})
    with pytest.raises(conv.Skip):
        conv.raw_expression({"len": 40})
    with pytest.raises(conv.Skip):
        conv.raw_expression({"bix": 8 * 702, "len": 8})


def _model(commands):
    return {"commands": commands}


def test_conversion_end_to_end(tmp_path):
    signal = lambda sid, name, fmt, path="Engine": {"id": sid, "name": name, "path": path, "fmt": fmt}  # noqa: E731
    models = {
        "Make-A": _model([
            {"hdr": "7E0", "rax": "7E8", "cmd": {"22": "1234"}, "freq": 5,
             "signals": [signal("A_OIL", "Engine oil temperature", {"len": 8, "add": -40, "max": 215, "unit": "celsius"})]},
            {"hdr": "7E1", "rax": "7E9", "cmd": {"21": "01"}, "freq": 1,
             "signals": [signal("A_GEAR", "Current gear", {"bix": 8, "len": 8, "max": 255,
                                                          "map": {"1": {"description": "First gear", "value": "1"}},
                                                          "unit": "scalar"}, "Transmission"),
                         signal("A_FAN", "Fan", {"bix": 21, "len": 1, "max": 1, "unit": "offon"}),
                         signal("A_CELL", "Cell 1", {"len": 8, "max": 255, "unit": "volts"}, "Battery.Cells")]},
            {"hdr": "710", "rax": "77A", "cmd": {"22": "0001"}, "freq": 1,     # another module: not supported
             "signals": [signal("A_ABS", "Wheel speed", {"len": 8, "max": 255, "unit": "kilometersPerHour"})]},
            {"hdr": "7E0", "rax": "7E8", "din": "03", "cmd": {"22": "2222"}, "freq": 1,   # needs a session
             "signals": [signal("A_X", "Secret", {"len": 8, "max": 255, "unit": "scalar"})]},
        ]),
        "Make-B": _model([
            {"hdr": "7E0", "rax": "7E8", "cmd": {"22": "1234"}, "freq": 5,
             "signals": [signal("B_OIL", "Engine oil temperature", {"len": 8, "add": -40, "max": 215, "unit": "celsius"})]},
        ]),
    }
    entries, skipped = conv.candidates(models)
    assert len(entries) == 3
    assert skipped["module outside 7E0-7E7"] == 1 and skipped["needs a session or other addressing"] == 1
    assert skipped["other topic (path)"] == 1
    params = conv.to_params(entries, 10, {"Engine oil temperature": "Temperatura oleju"})
    oil = next(p for p in params if p["request"] == "221234")
    assert oil["label_pl"] == "Temperatura oleju" and oil["unit"] == "°C" and oil["interval"] == 5
    assert "2 model(s)" in oil["note"] and "max" not in oil          # 215 is just the raw maximum
    gear = next(p for p in params if p["key"] == "CURRENT_GEAR")
    assert gear["header"] == "7E1" and gear["map"] == {1: "First gear"}
    fan = next(p for p in params if p["key"] == "FAN")
    assert fan["map"] == {0: "off", 1: "on"} and fan["formula"] == "bit(C,2)"

    text = conv.render("test-make", "Test make", ["ABC"], [], sorted(models), params)
    parsed = custom_pids.parse_set(text)
    assert parsed.license == "CC BY-SA 4.0" and parsed.match["wmi"] == ["ABC"]
    by_name = {p.name: p for p in parsed.params}
    assert by_name["ENGINE_OIL_TEMPERATURE"].decode(bytes([130])) == 90
    assert by_name["FAN"].decode(bytes([0, 0, 0b00000100])) == "on"        # bit 21 counted from the top
    assert by_name["FAN"].decode(bytes([0, 0, 0b11111011])) == "off"
    assert by_name["CURRENT_GEAR"].decode(bytes([0, 1])) == "First gear"


def test_command_line(tmp_path, capsys):
    model = {"commands": [{"hdr": "7E0", "rax": "7E8", "cmd": {"22": "F40D"}, "freq": 1, "signals": [
        {"id": "X_SPEED", "name": "Fuel temperature", "path": "Fuel", "fmt": {"len": 8, "add": -40, "max": 215, "unit": "celsius"}}]}]}
    source = tmp_path / "Make-Model.json"
    source.write_text(json.dumps(model))
    out = tmp_path / "make.toml"
    assert conv.main(["--id", "make", "--name", "Make", "--wmi", "XYZ", "-o", str(out), str(source)]) == 0
    parsed = custom_pids.parse_set(out.read_text(encoding="utf-8"))
    assert [p.request for p in parsed.params] == ["22F40D"] and "Make-Model" in out.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        conv.main(["--id", "Bad Id", "--name", "x", "-o", str(out), str(source)])


def test_shipped_obdb_sets_name_their_license_and_source():
    sets = {s.id: s for s in custom_pids.load_sets(None)[0]}
    for set_id in ("vw-group", "toyota", "hyundai-kia", "renault-dacia"):
        found = sets[set_id]
        assert found.license == "CC BY-SA 4.0" and "OBDb" in found.source and found.match.get("wmi")
        assert all(p.status == "unverified" and p.label_pl for p in found.params)
        assert all(p.header in custom_pids.HEADERS for p in found.params)
