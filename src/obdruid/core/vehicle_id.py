"""Recognising a car that reports no VIN, by its "fingerprint".

Many older cars (and some newer ones) do not answer the VIN request (service 09 PID 02). Then the
only things that tell two cars apart are how they talk to the adapter: the OBD protocol, the
modules that answer a request (engine, gearbox... by their address) and the set of standard PIDs
the engine control unit supports. Two different models rarely share all three; the same car gives
the same answer every time.

The PID set is compared with some tolerance: over Bluetooth a "supported PIDs" mask now and then
gets lost, and the program then knows fewer PIDs than the car has.
"""

from __future__ import annotations

from typing import Iterable, Optional

FORMAT = 1
MIN_PID_SIMILARITY = 0.85     # share of PIDs both fingerprints have, of all PIDs either has


def make_fingerprint(protocol: Optional[str], ecus: Iterable[str], pids: Iterable[int]) -> Optional[dict]:
    """Fingerprint stored in the vehicle profile; None when there is nothing to recognise the car by."""
    pid_list = sorted({int(p) for p in pids if 0 < int(p) < 256})
    if not pid_list:
        return None
    bitmap = 0
    for pid in pid_list:
        bitmap |= 1 << (255 - pid)
    return {"format": FORMAT, "protocol": str(protocol or ""), "ecus": sorted({str(e).upper() for e in ecus}),
            "pids": f"{bitmap:064X}"}


def _pids(fingerprint: dict) -> set[int]:
    try:
        bitmap = int(fingerprint.get("pids") or "0", 16)
    except ValueError:
        return set()
    return {pid for pid in range(1, 256) if bitmap >> (255 - pid) & 1}


def same_car(a: Optional[dict], b: Optional[dict]) -> Optional[bool]:
    """True / False when the fingerprints say the same / another car; None when one of them is missing."""
    if not a or not b or a.get("format") != FORMAT or b.get("format") != FORMAT:
        return None
    if a.get("protocol") and b.get("protocol") and a["protocol"] != b["protocol"]:
        return False
    if a.get("ecus") and b.get("ecus") and a["ecus"] != b["ecus"]:
        return False
    first, second = _pids(a), _pids(b)
    if not first or not second:
        return None
    return len(first & second) / len(first | second) >= MIN_PID_SIMILARITY


def describe(fingerprint: Optional[dict]) -> str:
    """Short text for the user, e.g. "protocol 6, modules E8 E9, 34 PIDs"."""
    if not fingerprint:
        return ""
    ecus = " ".join(fingerprint.get("ecus") or []) or "?"
    return f"protocol {fingerprint.get('protocol') or '?'}, modules {ecus}, {len(_pids(fingerprint))} PIDs"
