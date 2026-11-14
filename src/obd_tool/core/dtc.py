"""Diagnostic trouble code helpers: encoding, decoding and descriptions."""

from __future__ import annotations

import re

from obd.codes import DTC as DTC_DESCRIPTIONS  # English descriptions of generic codes

_LETTERS = "PCBU"
_CODE_RE = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")


def encode(code: str) -> bytes:
    """Encode a DTC like 'P0133' into its two-byte OBD form."""
    code = code.upper()
    if not _CODE_RE.match(code):
        raise ValueError(f"Invalid DTC: {code!r}")
    first = (_LETTERS.index(code[0]) << 6) | (int(code[1]) << 4) | int(code[2], 16)
    second = int(code[3:5], 16)
    return bytes([first, second])


def decode(data: bytes) -> str | None:
    """Decode two OBD bytes into a DTC string; 0000 means 'no code'."""
    a, b = data[0], data[1]
    if a == 0 and b == 0:
        return None
    return f"{_LETTERS[a >> 6]}{(a >> 4) & 0x3}{a & 0xF:X}{b:02X}"


_SYSTEMS_P = {
    "0": "Fuel and air metering, auxiliary emission controls",
    "1": "Fuel and air metering",
    "2": "Fuel and air metering (injector circuit)",
    "3": "Ignition system or misfire",
    "4": "Auxiliary emission controls",
    "5": "Vehicle speed, idle control, auxiliary inputs",
    "6": "Computer and output circuits",
    "7": "Transmission",
    "8": "Transmission",
    "9": "Transmission",
    "A": "Hybrid propulsion",
    "B": "Hybrid propulsion",
    "C": "Hybrid propulsion",
}

_AREAS = {"P": "Powertrain", "C": "Chassis", "B": "Body", "U": "Network"}


def system_of(code: str) -> str:
    """Name of the area/system a DTC belongs to."""
    code = code.upper()
    area = _AREAS.get(code[:1], "Unknown")
    if code[0] == "P":
        # P1xxx and P30xx-P33xx are manufacturer specific, the rest is SAE generic
        manufacturer = code[1] == "1" or (code[1] == "3" and code[2] in "0123")
    else:
        # B1/B2, C1/C2, U1/U2 are manufacturer specific
        manufacturer = code[1] in "12"
    prefix = f"{area}, manufacturer specific" if manufacturer else area
    if code[0] == "P" and not manufacturer:
        return f"{prefix}: {_SYSTEMS_P.get(code[2:3], 'other')}"
    return prefix


def describe(code: str) -> str:
    """English description of a generic code, or empty string if unknown."""
    return clean_description(DTC_DESCRIPTIONS.get(code.upper(), ""))


def clean_description(text: str) -> str:
    # python-OBD 0.7.x has a global rename artefact: "Response" -> "OBDResponse"
    return (text or "").replace("OBDResponse", "Response").strip()
