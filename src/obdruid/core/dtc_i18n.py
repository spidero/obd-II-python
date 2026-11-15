"""Translations of the generic (SAE J2012) trouble code descriptions.

The ~1800 English descriptions are built from a limited vocabulary, so they are translated by parts:
    [prefix] component [location] [fault]
e.g. "Fuel Rail Pressure Sensor Circuit Low (Bank 1)" -> component "Fuel Rail Pressure Sensor",
fault "Circuit Low", location "Bank 1" -> "Czujnik ciśnienia paliwa w listwie – obwód: sygnał niski (bank 1)".
The dictionaries live in dtc_<lang>.json: {"components": {...}, "faults": {...}, "prefixes": {...},
"locations": {...}, "full": {...}}. Anything not covered stays in English.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

FAULTS = [
    "Control Circuit Range/Performance", "Circuit Range/Performance", "Circuit Intermittent/Erratic",
    "Circuit Intermittent Input", "Circuit Low Input", "Circuit High Input", "Circuit Low Voltage",
    "Circuit High Voltage", "Control Circuit/Open", "Control Circuit Low", "Control Circuit High",
    "Control Circuit Open", "Circuit Intermittent", "Circuit Malfunction", "Circuit No Signal", "Circuit/Open",
    "Circuit Open", "Circuit Shorted", "Circuit Stuck", "Circuit Input", "Circuit Range", "Circuit Low",
    "Circuit High", "Control Circuit", "Circuit", "Performance or Stuck Off", "Performance or Stuck Open",
    "Range/Performance", "Performance", "Malfunction", "Intermittent/Erratic", "Intermittent", "Stuck Open",
    "Stuck Closed", "Stuck On", "Stuck Off", "Signal Low", "Signal High", "Low Input", "High Input", "Too Low",
    "Too High", "Low", "High", "Electrical", "No Signal", "Erratic", "Open", "Signal", "Input", "Output",
    "Efficiency Below Threshold", "Correlation", "Sense",
]
FAULTS.sort(key=len, reverse=True)
PREFIXES = ["Lost Communication With", "Invalid Data Received From", "Software Incompatibility With"]

_QUOTED = re.compile(r"""['"]([A-Z])(?:['"]|(?=$|[ /]))""")
_TRAILING_LETTER = re.compile(r" ([A-J])(?=$| \(|/| -)")
_NUMBERED = re.compile(r"\b(Bank|Sensor|Cylinder|Unit|Stage|Row|Group|Gear|Fan|Regulator) #?(\d+)\b")
_BUS_STATE = re.compile(r" \((High|Low|Open|Performance|shorted to Bus(?: [A-E])?)\)$")
_LOCATION = re.compile(r"\s*[-(]?\s*((?:Bank \d+(?:\s*,?\s*Sensor \d+)?)|(?:Cylinder \d+))\)?$")
_SPACES = re.compile(r"\s+")


def _unquote(text: str) -> str:
    """Drop quotes around a whole name: 'Door Control Module A' (but not around a single letter)."""
    if len(text) > 3 and text[0] in "'\"" and text[-1] == text[0] and text[2] != text[0]:
        return text[1:-1]
    return text


def split(description: str) -> tuple[str, str, str, str, list[str]]:
    """-> (prefix, component, fault, location, params); component has {1}, {2}... for letters and numbers."""
    text = _SPACES.sub(" ", description.replace("lntake", "Intake").replace("Circuit /Open", "Circuit/Open")).strip()
    text = text.rstrip(" /-").strip()
    text = _unquote(text)
    location = ""
    match = _LOCATION.search(text)
    if match and match.start() > 0:
        location, text = match.group(1), text[:match.start()].rstrip(" -(")
    prefix = ""
    for candidate in PREFIXES:
        if text.lower().startswith(candidate.lower() + " "):
            prefix, text = candidate, _unquote(text[len(candidate) + 1:])
            break
    fault = ""
    bus = _BUS_STATE.search(text)
    if bus:
        fault, text = "(" + re.sub(r" [A-E]$", "", bus.group(1)) + ")", text[:bus.start()]
    else:
        for candidate in FAULTS:
            if text.endswith(" " + candidate):
                fault, text = candidate, text[:-len(candidate) - 1]
                break
    params: list[str] = []

    def take(match: re.Match, template: str) -> str:
        params.append(match.group(match.lastindex))
        return template.format(n=len(params), head=match.group(1))

    text = _QUOTED.sub(lambda m: take(m, "{{{n}}}"), text)
    text = _TRAILING_LETTER.sub(lambda m: take(m, " {{{n}}}"), text)
    text = _NUMBERED.sub(lambda m: take(m, "{head} {{{n}}}"), text)
    return prefix, _SPACES.sub(" ", text).strip(" '\"-/"), fault, location, params


@lru_cache(maxsize=4)
def _catalog(lang: str) -> dict:
    path = Path(__file__).with_name(f"dtc_{lang}.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _location(location: str, catalog: dict) -> str:
    out = location
    for english, local in (catalog.get("locations") or {}).items():
        out = re.sub(rf"\b{english}\b", local, out)
    return out


def translate(description: str, lang: str) -> str:
    """The description in `lang`; parts that are not in the dictionary stay in English."""
    if not description or lang == "en":
        return description
    catalog = _catalog(lang)
    if not catalog:
        return description
    if description in catalog.get("full", {}):
        return catalog["full"][description]
    prefix, component, fault, location, params = split(description)
    local = (catalog.get("components") or {}).get(component)
    if local is None:
        return description
    local = re.sub(r"\{(\d)\}", lambda m: params[int(m.group(1)) - 1] if int(m.group(1)) <= len(params) else "", local)
    if prefix:
        local = (catalog.get("prefixes") or {}).get(prefix, prefix + " {x}").replace("{x}", local)
    if fault:
        local += (catalog.get("faults") or {}).get(fault, " – " + fault)
    if location:
        local += f" ({_location(location, catalog)})"
    return local


def coverage(lang: str) -> tuple[int, int, list[str]]:
    """(translated, total, untranslated descriptions) over python-OBD's code table."""
    from obd.codes import DTC

    from .dtc import clean_description

    descriptions = sorted({clean_description(v) for v in DTC.values()})
    missing = [d for d in descriptions if translate(d, lang) == d]
    return len(descriptions) - len(missing), len(descriptions), missing
