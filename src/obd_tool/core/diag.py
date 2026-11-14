"""OBD-II services python-OBD covers poorly: freeze frame (02), monitor tests (06), permanent codes (0A).

Everything here works on raw response bytes. `query` is a callable taking a hex
command string ("020C00") and returning the response payloads (service id first),
one per answering ECU, engine ECU first - see ObdSession._raw.
"""

from __future__ import annotations

from typing import Callable

import obd
from obd.UnitsAndScaling import UAS_IDS

from . import dtc as dtc_codes
from .models import Dtc, FreezeFrame, MonitorReport, MonitorTest, Reading
from .pids import NOT_LIVE, param_for, unit_name

Query = Callable[[str], list[bytes]]

# PIDs tried when the ECU does not report which freeze frame PIDs it supports
_FREEZE_FALLBACK = [0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11]

NEAR_ONE_SIDED = 15.0   # % headroom below which a test with a single limit is "near"
NEAR_TWO_SIDED = 10.0   # % of the allowed window


def _first(responses: list[bytes], *prefix: int) -> bytes | None:
    """First payload that starts with the expected bytes (positive response to our request)."""
    want = bytes(prefix)
    for data in responses:
        if data[:len(want)] == want:
            return data
    return None


def _supported(query_mask: Callable[[int], bytes | None], limit: int = 0xE0) -> list[int] | None:
    """Walk the 'supported ids' bitmasks (00, 20, 40...). None when the ECU does not answer at all."""
    ids: list[int] = []
    base = 0
    while base <= limit:
        mask = query_mask(base)
        if mask is None or len(mask) < 4:
            return ids if base else None
        bits = int.from_bytes(mask[:4], "big")
        found = [base + n for n in range(1, 0x21) if bits & (1 << (32 - n))]
        ids.extend(found)
        if base + 0x20 not in found:
            break
        base += 0x20
    return ids


# --------------------------------------------------------------------------- mode 0A / 02
def read_permanent(query: Query) -> list[Dtc] | None:
    """Permanent codes (mode 0A). None when the car does not support the service."""
    data = _first(query("0A"), 0x4A)
    if data is None:
        return None
    body = data[2:]  # skip the service id and the code count
    out = []
    for n in range(0, len(body) - 1, 2):
        code = dtc_codes.decode(body[n:n + 2])
        if code:
            out.append(Dtc(code=code, description=dtc_codes.describe(code), kind="permanent",
                           system=dtc_codes.system_of(code)))
    return out


def _decode_live(pid: int, payload: bytes):
    """Decode a mode 01 style payload with python-OBD's decoder. Returns (Param, value) or None."""
    if pid >= len(obd.commands[1]):
        return None
    cmd = obd.commands[1][pid]
    if cmd.name in NOT_LIVE:
        return None

    class _Msg:  # the decoders only look at .data
        data = bytearray([0x41, pid]) + bytearray(payload)

    msg = _Msg()
    if cmd.bytes and len(msg.data) < cmd.bytes:
        msg.data += bytes(cmd.bytes - len(msg.data))
    try:
        value = cmd.decode([msg])
    except Exception:  # a decoder choking on odd data must not break the whole frame
        return None
    return param_for(cmd), value


def read_freeze_frame(query: Query, plain_value, frame: int = 0) -> FreezeFrame:
    """Freeze frame `frame` (mode 02). `plain_value` converts python-OBD values to plain data."""
    suffix = f"{frame:02X}"
    data = _first(query("0202" + suffix), 0x42, 0x02)
    if data is None or len(data) < 5:
        return FreezeFrame(supported=False)
    code = dtc_codes.decode(data[3:5])
    if code is None:
        return FreezeFrame(supported=True)  # the ECU answers, but no frame is stored
    result = FreezeFrame(supported=True, dtc=Dtc(code=code, description=dtc_codes.describe(code),
                                                 kind="freeze", system=dtc_codes.system_of(code)))

    def mask(base: int) -> bytes | None:
        answer = _first(query(f"02{base:02X}{suffix}"), 0x42, base)
        return answer[3:] if answer else None

    pids = _supported(mask, limit=0x40)
    if not pids:
        pids = _FREEZE_FALLBACK
    for pid in pids:
        if pid == 0x02 or pid % 0x20 == 0:
            continue
        answer = _first(query(f"02{pid:02X}{suffix}"), 0x42, pid)
        if answer is None:
            continue
        decoded = _decode_live(pid, answer[3:])
        if decoded is None:
            continue
        param, raw = decoded
        value, unit = plain_value(raw)
        if value is None:
            continue
        if isinstance(value, float):
            value = round(value, param.decimals)
        result.readings.append(Reading(key=param.key, label=param.label, value=value,
                                       unit=param.unit or unit, pid=f"02{pid:02X}"))
    return result


# --------------------------------------------------------------------------- mode 06
_TEST_NAMES = {
    0x01: "Rich to lean sensor threshold voltage",
    0x02: "Lean to rich sensor threshold voltage",
    0x03: "Low sensor voltage for switch time calculation",
    0x04: "High sensor voltage for switch time calculation",
    0x05: "Rich to lean sensor switch time",
    0x06: "Lean to rich sensor switch time",
    0x07: "Minimum sensor voltage for test cycle",
    0x08: "Maximum sensor voltage for test cycle",
    0x09: "Time between sensor transitions",
    0x0A: "Sensor period",
    0x0B: "Misfires, average of last 10 cycles",
    0x0C: "Misfires, current drive cycle",
}


def _monitor_names() -> dict[int, str]:
    names: dict[int, str] = {}
    for bank in range(4):
        for sensor in range(4):
            n = bank * 4 + sensor
            names[0x01 + n] = f"O2 sensor B{bank + 1}S{sensor + 1}"
            names[0x41 + n] = f"O2 sensor heater B{bank + 1}S{sensor + 1}"
        names[0x21 + bank] = f"Catalyst bank {bank + 1}"
        names[0x31 + bank] = f"EGR bank {bank + 1}"
        names[0x35 + bank] = f"VVT bank {bank + 1}"
        names[0x61 + bank] = f"Heated catalyst bank {bank + 1}"
        names[0x71 + bank] = f"Secondary air {bank + 1}"
        names[0x81 + bank] = f"Fuel system bank {bank + 1}"
    names.update({
        0x39: "EVAP (cap off)", 0x3A: "EVAP (0.090 in leak)", 0x3B: "EVAP (0.040 in leak)",
        0x3C: "EVAP (0.020 in leak)", 0x3D: "EVAP purge flow",
        0x85: "Boost pressure bank 1", 0x86: "Boost pressure bank 2",
        0x90: "NOx adsorber bank 1", 0x91: "NOx adsorber bank 2",
        0x98: "NOx catalyst bank 1", 0x99: "NOx catalyst bank 2",
        0xA1: "Misfire (general)", 0xB0: "Particulate filter bank 1", 0xB1: "Particulate filter bank 2",
    })
    for cyl in range(1, 13):
        names[0xA1 + cyl] = f"Misfire cylinder {cyl}"
    return names


MONITOR_NAMES = _monitor_names()
MISFIRE_MIDS = range(0xA1, 0xAE)


def monitor_name(mid: int) -> str:
    return MONITOR_NAMES.get(mid, f"Monitor ${mid:02X}")


def test_name(tid: int) -> str:
    return _TEST_NAMES.get(tid, f"Manufacturer test ${tid:02X}")


def rate(value: float, lo: float | None, hi: float | None) -> tuple[str, float | None]:
    """Rate a value against its limits: (status, headroom in %)."""
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        return "fail", 0.0
    if lo is None and hi is None:
        return "pass", None
    if lo is not None and hi is not None:
        span = hi - lo
        if span <= 0:
            return "pass", None
        margin = min(value - lo, hi - value) / span * 100
        return ("near" if margin < NEAR_TWO_SIDED else "pass"), round(margin, 1)
    if hi is not None:
        margin = (hi - value) / abs(hi) * 100 if hi else None
    else:
        margin = (value - lo) / abs(lo) * 100 if lo else None
    if margin is None:
        return "pass", None
    margin = min(100.0, margin)
    return ("near" if margin < NEAR_ONE_SIDED else "pass"), round(margin, 1)


def parse_monitor(data: bytes) -> list[MonitorTest]:
    """Parse a mode 06 response payload (46, then 9-byte blocks: MID TID UAS value min max)."""
    tests = []
    body = data[1:]
    for n in range(0, len(body) - 8, 9):
        mid, tid, uas_id = body[n], body[n + 1], body[n + 2]
        raw = [body[n + 3:n + 5], body[n + 5:n + 7], body[n + 7:n + 9]]
        if not any(any(part) for part in raw):
            continue  # all zero: the ECU has not run this test yet
        uas = UAS_IDS.get(uas_id)
        if uas is None:
            continue

        def conv(part: bytes) -> float:
            number = int.from_bytes(part, "big", signed=uas.signed)
            return round(number * uas.scale + uas.offset, 4)

        no_lower = bytes(raw[1]) == (b"\x80\x00" if uas.signed else b"\x00\x00")
        no_upper = bytes(raw[2]) == (b"\x7f\xff" if uas.signed else b"\xff\xff")
        value = conv(raw[0])
        lo = None if no_lower else conv(raw[1])
        hi = None if no_upper else conv(raw[2])
        status, margin = rate(value, lo, hi)
        unit = unit_name(str(uas.unit))
        tests.append(MonitorTest(mid=mid, tid=tid, monitor=monitor_name(mid), name=test_name(tid),
                                 value=value, min=lo, max=hi, unit={"count": ""}.get(unit, unit),
                                 status=status, margin=margin))
    return tests


def read_monitors(query: Query) -> MonitorReport:
    """On-board monitor test results (mode 06, CAN format)."""
    def mask(base: int) -> bytes | None:
        answer = _first(query(f"06{base:02X}"), 0x46, base)
        return answer[2:] if answer else None

    mids = _supported(mask)
    if mids is None:
        return MonitorReport(supported=False)
    report = MonitorReport(supported=True)
    for mid in mids:
        if mid % 0x20 == 0:
            continue
        answer = _first(query(f"06{mid:02X}"), 0x46, mid)
        if answer is not None:
            report.tests.extend(parse_monitor(answer))
    return report
