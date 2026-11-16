#!/usr/bin/env python3
"""Convert OBDb signal sets (https://github.com/OBDb) into an OBDruid PID set file (.toml).

OBDb keeps one repository per car model with `signalsets/v3/default.json`: the manufacturer
requests (UDS 22 / KWP 21), the module address, and for every signal the bit position, length,
scaling and unit. This script merges the files of several models of one make (or platform),
keeps what OBDruid can ask for (modules 7E0-7E7, services 21 and 22, no extended addressing or
diagnostic sessions), ranks the signals by how many models share them and writes the most common
ones as an OBDruid PID set.

The data of OBDb is licensed CC BY-SA 4.0, and so is every file this script writes.
Nothing here is verified on a car: the program probes the parameters and keeps what answers.

Examples:
    # from local copies of default.json files (named after the repository: Volkswagen-Golf.json)
    python tools/obdb_convert.py --id vag --name "VW group" --wmi WVW,WAU -o vag.toml data/*.json

    # download every repository whose name starts with the given makes (raw files, no token needed;
    # set GITHUB_TOKEN to raise the API limit for the repository list)
    python tools/obdb_convert.py --id renault-dacia --name "Renault / Dacia" --fetch Renault,Dacia \
        --wmi VF1,UU1 -o renault-dacia.toml

The script needs only the standard library. When obdruid is installed, the written file is also
checked with the program's own parser.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

HEADERS = tuple(f"7E{n:X}" for n in range(8))
SERVICES = ("21", "22")
# commands that need something OBDruid does not do: extended or 29-bit addressing, a diagnostic
# session, another tester address, a protocol or baud rate of its own
UNSUPPORTED = ("eax", "din", "pri", "tst", "proto", "baud", "init")
# signal paths worth showing in a workshop tool; EV battery cells, trip counters, tyres etc. are left out
DEFAULT_PATHS = ("Engine", "Transmission", "Fuel", "Fluids", "Control.Pedals", "Drivetrain")

# OBDb unit -> (OBDruid unit, factor, offset) applied to the scaled value
UNITS = {
    "scalar": ("", 1, 0), "unknown": ("", 1, 0), None: ("", 1, 0),
    "percent": ("%", 1, 0), "celsius": ("°C", 1, 0), "fahrenheit": ("°C", 5 / 9, -32 * 5 / 9),
    "volts": ("V", 1, 0), "millivolts": ("mV", 1, 0), "amps": ("A", 1, 0), "milliamps": ("mA", 1, 0),
    "ohms": ("Ω", 1, 0), "kiloohms": ("kΩ", 1, 0), "megaohms": ("MΩ", 1, 0), "milliohms": ("mΩ", 1, 0),
    "degrees": ("°", 1, 0), "rpm": ("rpm", 1, 0), "hertz": ("Hz", 1, 0),
    "seconds": ("s", 1, 0), "milliseconds": ("ms", 1, 0), "minutes": ("min", 1, 0), "hours": ("h", 1, 0),
    "newtonMeters": ("Nm", 1, 0), "watts": ("W", 1, 0), "kilowatts": ("kW", 1, 0),
    "kilowattHours": ("kWh", 1, 0), "ampereHours": ("Ah", 1, 0),
    "bars": ("bar", 1, 0), "kilopascal": ("kPa", 1, 0), "psi": ("kPa", 6.894757, 0),
    "kilometers": ("km", 1, 0), "miles": ("km", 1.609344, 0), "meters": ("m", 1, 0),
    "centimeters": ("cm", 1, 0), "millimeters": ("mm", 1, 0),
    "kilometersPerHour": ("km/h", 1, 0), "milesPerHour": ("km/h", 1.609344, 0),
    "metersPerSecondSquared": ("m/s²", 1, 0), "gravity": ("g", 1, 0),
    "liters": ("l", 1, 0), "litersPerHour": ("l/h", 1, 0), "gramsPerSecond": ("g/s", 1, 0),
    "kilogramsPerHour": ("kg/h", 1, 0), "gramsPerLiter": ("g/l", 1, 0),
}
STATES = {"offon": {0: "off", 1: "on"}, "noyes": {0: "no", 1: "yes"}}

RAW_URL = "https://raw.githubusercontent.com/OBDb/{repo}/main/signalsets/v3/default.json"
LIST_URL = "https://api.github.com/orgs/OBDb/repos?per_page=100&page={page}"
LICENSE = "CC BY-SA 4.0"
MAX_PARAMS = 300


class Skip(Exception):
    """The signal cannot be expressed in OBDruid; the message says why."""


# --------------------------------------------------------------------------- decoding
def reference_value(fmt: dict, data: bytes) -> Optional[float]:
    """Value of an OBDb signal straight from its definition (used by the tests to check the formulas).

    `data` are the bytes after the echoed service and identifier; bit 0 is the most significant
    bit of the first byte.
    """
    bix, length = fmt.get("bix", 0), fmt["len"]
    if (bix + length + 7) // 8 > len(data):
        return None
    if fmt.get("blsb"):
        raw = int.from_bytes(data[bix // 8:(bix + length) // 8], "little")
    else:
        whole = int.from_bytes(data, "big")
        raw = (whole >> (len(data) * 8 - bix - length)) & ((1 << length) - 1)
    if fmt.get("sign") and raw >= 1 << (length - 1):
        raw -= 1 << length
    return raw * fmt.get("mul", 1) / fmt.get("div", 1) + fmt.get("add", 0)


def byte_name(index: int) -> str:
    """Formula letter of a data byte, as in obdruid.core.custom_pids: 0 -> A, 25 -> Z, 26 -> AA..."""
    if index < 26:
        return chr(ord("A") + index)
    first, second = divmod(index - 26, 26)
    return chr(ord("A") + first) + chr(ord("A") + second)


def _letters(first: int, last: int) -> list[str]:
    return [byte_name(i) for i in range(first, last + 1)]


def _number(value: float) -> str:
    """Shortest exact-enough text of a constant."""
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


def raw_expression(fmt: dict) -> str:
    """Formula text for the raw (unscaled, but signed) value of an OBDb signal."""
    bix, length = fmt.get("bix", 0), fmt["len"]
    if length < 1 or length > 32:
        raise Skip(f"length of {length} bits")
    first, last = bix // 8, (bix + length - 1) // 8
    if last > 701:   # formula letters end at ZZ
        raise Skip("beyond byte 702 of the answer")
    aligned = bix % 8 == 0 and length % 8 == 0
    signed = bool(fmt.get("sign"))
    if fmt.get("blsb"):
        if not aligned:
            raise Skip("little-endian value that is not whole bytes")
        letters = list(reversed(_letters(first, last)))        # most significant first
    else:
        letters = _letters(first, last)
    if aligned:
        if signed:
            return f"signed({','.join(letters)})"
        if len(letters) == 1:
            return letters[0]
        return "(" + "+".join(f"{letter}*{256 ** (len(letters) - 1 - i)}" if i < len(letters) - 1 else letter
                              for i, letter in enumerate(letters)) + ")"
    span = len(letters)
    if span > 4:
        raise Skip("bit field across more than 4 bytes")
    shift = span * 8 - (bix % 8) - length
    if length == 1 and span == 1 and not signed:
        return f"bit({letters[0]},{shift})"
    joined = letters[0] if span == 1 else "(" + "+".join(
        f"{letter}*{256 ** (span - 1 - i)}" if i < span - 1 else letter for i, letter in enumerate(letters)) + ")"
    value = f"({joined}>>{shift})" if shift else joined
    value = f"({value}&{(1 << length) - 1})"
    if signed:   # two's complement of a bit field: flip the sign bit, then take its weight away
        half = 1 << (length - 1)
        value = f"(({value}^{half})-{half})"
    return value


def formula(fmt: dict, factor: float = 1, offset: float = 0) -> str:
    """Complete OBDruid formula: raw value, OBDb scaling, then the unit conversion."""
    text = raw_expression(fmt)
    mul, div, add = fmt.get("mul", 1), fmt.get("div", 1), fmt.get("add", 0)
    if factor != 1 or offset:
        # fold the unit conversion into one multiplier and one offset
        mul, div, add = mul * factor / div, 1, add * factor + offset
    if mul != 1:
        text += f"*{_number(mul)}"
    if div != 1:
        text += f"/{_number(div)}"
    if add:
        text += f"{'+' if add > 0 else '-'}{_number(abs(add))}"
    return text


def _full_scale(fmt: dict, low: bool = False) -> bool:
    """True when the source's max (or min) is just the largest (smallest) raw number scaled -
    OBDb often fills the limits in that way."""
    signed = bool(fmt.get("sign"))
    if low:
        if fmt.get("min") is None:
            return False
        raw, limit = (-(1 << (fmt["len"] - 1)) if signed else 0), fmt["min"]
    else:
        raw, limit = (1 << (fmt["len"] - (1 if signed else 0))) - 1, fmt["max"]
    full = raw * fmt.get("mul", 1) / fmt.get("div", 1) + fmt.get("add", 0)
    if low and not signed:
        return False         # an unsigned minimum is just the offset: fine as the bottom of a gauge
    return abs(limit - full) <= max(1.0, abs(full) * 0.01)


def decimals(fmt: dict, factor: float = 1) -> int:
    step = abs(fmt.get("mul", 1) / fmt.get("div", 1) * factor)
    if step == 0 or step >= 1:
        return 0
    return min(3, max(0, math.ceil(-math.log10(step) - 1e-9)))


# --------------------------------------------------------------------------- reading OBDb
def fetch_list(prefixes: Iterable[str]) -> list[str]:
    """Names of the OBDb repositories whose name is one of the makes or starts with "<make>-"."""
    names, page = [], 1
    while True:
        request = urllib.request.Request(LIST_URL.format(page=page), headers=_headers())
        with urllib.request.urlopen(request, timeout=30) as answer:
            batch = json.load(answer)
        if not batch:
            break
        names += [r["name"] for r in batch]
        page += 1
    wanted = tuple(prefixes)
    return sorted(n for n in names if any(n == p or n.startswith(p + "-") for p in wanted))


def _headers() -> dict:
    headers = {"User-Agent": "obdruid-obdb-convert"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers


def fetch_signalset(repo: str) -> Optional[dict]:
    request = urllib.request.Request(RAW_URL.format(repo=repo), headers={"User-Agent": "obdruid-obdb-convert"})
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return json.load(answer)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- merging
def _path_wanted(path: Optional[str], paths: tuple[str, ...]) -> bool:
    return bool(path) and any(path == p or path.startswith(p + ".") for p in paths)


def candidates(models: dict[str, dict], paths: tuple[str, ...] = DEFAULT_PATHS) -> tuple[list[dict], collections.Counter]:
    """Signals OBDruid can read, merged over the models (same request, position and scaling = one signal)."""
    merged: dict[tuple, dict] = {}
    skipped: collections.Counter = collections.Counter()
    for model, data in sorted(models.items()):
        for command in data.get("commands", []):
            header = str(command.get("hdr", "")).upper()
            (service, ident), = command["cmd"].items() if len(command.get("cmd", {})) == 1 else ((None, None),)
            if header not in HEADERS:
                skipped["module outside 7E0-7E7"] += len(command.get("signals", []))
                continue
            if service not in SERVICES:
                skipped[f"service {service}"] += len(command.get("signals", []))
                continue
            reply = command.get("rax")
            if reply and reply.upper() != f"7E{int(header[2], 16) + 8:X}":
                skipped["answer from another address"] += len(command.get("signals", []))
                continue
            if any(k in command for k in UNSUPPORTED):
                skipped["needs a session or other addressing"] += len(command.get("signals", []))
                continue
            request = f"{service}{ident}".upper()
            for signal in command.get("signals", []):
                if not _path_wanted(signal.get("path"), paths) or signal.get("hidden"):
                    skipped["other topic (path)"] += 1
                    continue
                fmt = signal["fmt"]
                unit = fmt.get("unit")
                if unit not in UNITS and unit not in STATES and not fmt.get("map"):
                    skipped[f"unit {unit}"] += 1
                    continue
                try:
                    raw_expression(fmt)
                except Skip as e:
                    skipped[str(e)] += 1
                    continue
                shape = tuple(fmt.get(k) for k in ("bix", "len", "blsb", "sign", "mul", "div", "add", "unit"))
                key = (header, request, shape, json.dumps(fmt.get("map"), sort_keys=True))
                entry = merged.setdefault(key, {"header": header, "request": request, "fmt": fmt, "names": collections.Counter(),
                                                "paths": collections.Counter(), "models": set(), "freq": [],
                                                "years": [], "descriptions": collections.Counter()})
                entry["names"][signal["name"].strip()] += 1
                entry["paths"][signal["path"]] += 1
                entry["models"].add(model)
                entry["freq"].append(command.get("freq", 1))
                if signal.get("description"):
                    entry["descriptions"][signal["description"].strip()] += 1
                if command.get("filter"):
                    entry["years"].append(command["filter"])
    return list(merged.values()), skipped


def _key(name: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "PARAM"
    if not base[0].isalpha():
        base = "P_" + base
    base = base[:27].rstrip("_")
    key, n = base, 2
    while key in used:
        key, n = f"{base}_{n}", n + 1
    used.add(key)
    return key


def _years_note(filters: list[dict]) -> str:
    if not filters:
        return ""
    starts = [f.get("from") for f in filters if f.get("from")]
    ends = [f.get("to") for f in filters if f.get("to")]
    singles = sorted({y for f in filters for y in f.get("years", [])})
    parts = []
    if starts or ends:
        parts.append(f"{min(starts) if starts else ''}-{max(ends) if ends else ''}")
    if singles:
        parts.append(", ".join(map(str, singles)))
    return f"Model years in the source: {'; '.join(parts)}." if parts else ""


# what a mechanic looks at first; counters, learned values and self-test flags last
USEFUL = re.compile(r"temperature|pressure|soot|particulate|dpf|regenerat|boost|turbo|oil|coolant|fuel|injection|"
                    r"injector|rail|gear|clutch|torque|knock|misfire|lambda|oxygen|air-fuel|exhaust|egr|throttle|"
                    r"mass air|load|battery voltage|glow|adblue|urea|nox|catalyst|trim|odometer|mileage|distance", re.I)
OBSCURE = re.compile(r"counter|count\b|adaptation|learn|programm|test result|status|switch|relay|"
                     r"synchroni|calibration|request|flag|mode\b|identifier|bias", re.I)


def score(entry: dict) -> float:
    """Rank of a candidate: models that share it, weighted by how useful its name sounds."""
    name = entry["names"].most_common(1)[0][0]
    weight = 1.0
    if USEFUL.search(name):
        weight *= 3
    if OBSCURE.search(name):
        weight /= 3
    return len(entry["models"]) * weight


def to_params(entries: list[dict], limit: int, labels_pl: dict[str, str]) -> list[dict]:
    """The `limit` best ranked signals (see score), as [[params]] tables, grouped by request."""
    chosen = sorted(entries, key=lambda e: (-score(e), e["request"], e["fmt"].get("bix", 0)))[:limit]
    order = sorted(chosen, key=lambda e: (e["header"], e["request"], e["fmt"].get("bix", 0)))
    used: set[str] = set()
    params = []
    for entry in order:
        fmt = entry["fmt"]
        name = entry["names"].most_common(1)[0][0][:80]
        param: dict = {"key": _key(name, used), "label": name}
        if labels_pl.get(name):
            param["label_pl"] = labels_pl[name][:80]
        param["request"] = entry["request"]
        if entry["header"] != "7E0":
            param["header"] = entry["header"]
        unit = fmt.get("unit")
        mapping = fmt.get("map")
        if mapping or unit in STATES:
            param["formula"] = formula(fmt)
            texts = STATES.get(unit) or {}
            if mapping:
                texts = {}
                for value, item in mapping.items():
                    text = str(item.get("value", "")) if isinstance(item, dict) else str(item)
                    if isinstance(item, dict) and (not text or text.lstrip("-").isdigit()):
                        text = str(item.get("description") or text)
                    texts[int(value)] = text[:60]
            param["map"] = texts
        else:
            target, factor, offset = UNITS[unit]
            param["formula"] = formula(fmt, factor, offset)
            if target:
                param["unit"] = target
            places = decimals(fmt, factor)
            if places:
                param["decimals"] = places
            low, high = fmt.get("min"), fmt.get("max")
            if high is not None and (_full_scale(fmt) or _full_scale(fmt, low=True)):
                high = None          # only the range of the raw number, not a useful gauge range
            if high is not None and math.isfinite(high):
                low = 0 if low is None else low
                low, high = low * factor + offset, high * factor + offset
                if high > low:
                    param["min"], param["max"] = float(f"{low:.4g}"), float(f"{high:.4g}")
        freq = max(entry["freq"]) if entry["freq"] else 1
        if freq >= 2:
            param["interval"] = min(3600, int(freq))
        models = sorted(entry["models"])
        note = f"OBDb: {len(models)} model(s), e.g. {', '.join(models[:3])}."
        years = _years_note(entry["years"])
        if years:
            note += " " + years
        if entry["descriptions"]:
            note = entry["descriptions"].most_common(1)[0][0][:200] + " " + note
        param["note"] = note[:400]
        params.append(param)
    return params


# --------------------------------------------------------------------------- writing
def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)      # JSON string escapes are valid TOML
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(type(value))


def _toml_key(key) -> str:
    key = str(key)
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def render(set_id: str, name: str, wmi: list[str], fuel: list[str], repos: list[str], params: list[dict],
           note: str = "") -> str:
    today = datetime.date.today().isoformat()
    lines = [
        f"# {name} - manufacturer parameters converted from OBDb.",
        "#",
        "# NOT VERIFIED ON A CAR. Generated by tools/obdb_convert.py from the community database OBDb",
        "# (https://github.com/OBDb, signalsets/v3/default.json of the repositories listed below).",
        "# Only modules 7E0-7E7 (engine, gearbox) with services 22 and 21; the parameters shared by",
        "# the most models come first. Switch the set on for a car: the program asks for every",
        "# parameter once and keeps the ones the car answers. Treat the values with care.",
        "#",
        f"# License of this file: {LICENSE} (https://creativecommons.org/licenses/by-sa/4.0/),",
        "# the license of OBDb. Changes made: merged over models, converted to the OBDruid PID set",
        "# format (bit positions to byte formulas, imperial units to metric), Polish labels added.",
        f"# Converted on {today} from {len(repos)} repositories:",
    ]
    line = "#  "
    for repo in repos:
        if len(line) + len(repo) + 2 > 100:
            lines.append(line.rstrip(","))
            line = "#  "
        line += f" {repo},"
    lines.append(line.rstrip(","))
    lines += [
        "format = 1",
        f"id = {_toml_value(set_id)}",
        f"name = {_toml_value(name)}",
        f"source = {_toml_value('OBDb (github.com/OBDb), converted with tools/obdb_convert.py; not verified on a car')}",
        f"license = {_toml_value(LICENSE)}",
        f"note = {_toml_value(note or 'Candidates from a community database, merged over many models and engines: a car answers only some of them. Probing shows which ones.')}",
        "",
        "[match]",
    ]
    if wmi:
        lines.append(f"wmi = {_toml_value(wmi)}")
    if fuel:
        lines.append(f"fuel = {_toml_value(fuel)}")
    for param in params:
        lines += ["", "[[params]]"]
        for key, value in param.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- command line
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", type=Path, help="local default.json copies, named <repository>.json")
    parser.add_argument("--fetch", default="", help="comma separated makes to download from OBDb, e.g. Renault,Dacia")
    parser.add_argument("--id", required=True, help="set id: lower case letters, digits and '-'")
    parser.add_argument("--name", required=True, help="set name shown in the program")
    parser.add_argument("--wmi", default="", help="comma separated VIN prefixes the set is suggested for")
    parser.add_argument("--fuel", default="", help="comma separated fuels (petrol, diesel...) to suggest the set for")
    parser.add_argument("--paths", default=",".join(DEFAULT_PATHS), help="OBDb signal paths to keep")
    parser.add_argument("--max", type=int, default=80, help="most parameters in the set (default 80)")
    parser.add_argument("--labels-pl", type=Path, help="JSON file: English label -> Polish label")
    parser.add_argument("--note", default="", help="note of the set")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.id):
        parser.error("--id may only contain lower case letters, digits and '-'")
    models: dict[str, dict] = {}
    for path in args.files:
        try:
            models[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"skipped {path}: {e}", file=sys.stderr)
    if args.fetch:
        for repo in fetch_list(m.strip() for m in args.fetch.split(",") if m.strip()):
            data = fetch_signalset(repo)
            if data:
                models[repo] = data
    if not models:
        parser.error("no signal sets: give files or --fetch")

    paths = tuple(p.strip() for p in args.paths.split(",") if p.strip())
    entries, skipped = candidates(models, paths)
    labels = json.loads(args.labels_pl.read_text(encoding="utf-8")) if args.labels_pl else {}
    params = to_params(entries, min(args.max, MAX_PARAMS), labels)
    if not params:
        print("No convertible signals.", file=sys.stderr)
        return 1
    split = lambda text: [t.strip() for t in text.split(",") if t.strip()]  # noqa: E731
    used = sorted({m for e in entries for m in e["models"]})
    text = render(args.id, args.name, split(args.wmi), split(args.fuel), used, params, args.note)

    try:
        from obdruid.core.custom_pids import parse_set   # the same checks the program runs on import
    except ImportError:
        print("obdruid is not installed here: the file was not checked; import it in the program to check it.",
              file=sys.stderr)
    else:
        parse_set(text)
    args.output.write_text(text, encoding="utf-8")
    missing = sorted({p["label"] for p in params if "label_pl" not in p})
    print(f"{args.output}: {len(params)} parameters from {len(used)} of {len(models)} models "
          f"({len(entries)} candidates); {len({p['request'] for p in params})} requests.")
    for reason, count in skipped.most_common():
        print(f"  skipped {count:6d} signals: {reason}")
    if labels and missing:
        print(f"  {len(missing)} labels without a Polish translation", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    sys.exit(main())
