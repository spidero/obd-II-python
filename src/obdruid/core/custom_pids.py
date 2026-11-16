"""Manufacturer specific parameters described in data files ("PID sets").

Standard mode 01 does not cover what many owners care about most, e.g. the soot load of a
diesel particulate filter. Car makers expose such values through UDS service 22
(ReadDataByIdentifier) under identifiers that differ per make, engine and model year, so
they live in TOML files instead of the code: built-in ones in ``core/pidsets`` and the
user's own in the data directory.

Read only by design: a set may only use the reading services 01, 21 (ReadDataByLocalIdentifier,
used by e.g. Hyundai/Kia and Toyota) and 22. Everything that writes to or controls a module
(2E, 2F, 31, 27, 10, 14, 11...) is refused when the file is loaded.

Formulas use the Torque convention - A, B, C... Z, AA, AB... are the data bytes of the answer -
and are evaluated by a small whitelist interpreter (never ``eval``).
"""

from __future__ import annotations

import ast
import math
import operator
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import Param

FORMAT = 1
BUILTIN_DIR = Path(__file__).parent / "pidsets"
KEY_PREFIX = "X_"
MAX_FILE_SIZE = 256 * 1024
MAX_PARAMS = 300

# service id -> length of the whole request in hex digits (service + identifier)
ALLOWED_SERVICES = {"01": 4, "21": 4, "22": 6}
# Physical request addresses on 11 bit CAN: engine, transmission and the other emission related modules.
# The adapter listens for 7E8-7EF by default, so these work without touching its receive filter.
HEADERS = tuple(f"7E{n:X}" for n in range(8))
DEFAULT_HEADER = "7E0"

STATUSES = ("verified", "unverified")
# what a parameter means, for the health check (any make): soot load in %, km since the last
# regeneration, regeneration running (a number > 0 or a state "active"/"on"/"yes")
ROLES = ("dpf_soot", "dpf_distance", "dpf_regen")
TILE_FIELDS = ("min", "max", "warn_low", "warn_high", "alarm_low", "alarm_high")

# UDS negative response codes worth explaining
NRC = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length",
    0x14: "response too long",
    0x22: "conditions not correct",
    0x31: "identifier not supported",
    0x33: "security access denied",
    0x7E: "not supported in the active session",
    0x7F: "service not supported in the active session",
}


class PidSetError(ValueError):
    """A PID set file is malformed or asks for something that is not allowed."""


# --------------------------------------------------------------------------- requests
def check_request(request: str) -> str:
    """Normalize a request ("22 f1 90" -> "22F190"); only read services pass."""
    text = re.sub(r"\s+", "", str(request)).upper()
    if not re.fullmatch(r"[0-9A-F]+", text) or len(text) < 4:
        raise PidSetError(f"Invalid request {request!r}: expected hex digits, e.g. 22F190")
    service = text[:2]
    if service not in ALLOWED_SERVICES:
        raise PidSetError(f"Service {service} is not allowed (request {text}): only reading with "
                          f"{', '.join(sorted(ALLOWED_SERVICES))} is supported")
    if len(text) != ALLOWED_SERVICES[service]:
        raise PidSetError(f"Invalid request {text}: service {service} takes "
                          f"{(ALLOWED_SERVICES[service] - 2) // 2} identifier byte(s)")
    return text


def check_header(header: Optional[str]) -> str:
    text = re.sub(r"\s+", "", str(header or DEFAULT_HEADER)).upper()
    if text not in HEADERS:
        raise PidSetError(f"Unsupported header {header!r}: use one of {HEADERS[0]}-{HEADERS[-1]}")
    return text


def byte_index(name: str) -> int:
    """Position of a formula letter in the data: A=0 ... Z=25, AA=26, AB=27 ... (as in Torque)."""
    if len(name) == 1:
        return ord(name) - ord("A")
    return 26 + (ord(name[0]) - ord("A")) * 26 + (ord(name[1]) - ord("A"))


def byte_name(index: int) -> str:
    """Formula letter of a data byte: 0 -> A, 25 -> Z, 26 -> AA..."""
    if index < 26:
        return chr(ord("A") + index)
    first, second = divmod(index - 26, 26)
    return chr(ord("A") + first) + chr(ord("A") + second)


def nrc_text(code: int) -> str:
    return NRC.get(code, f"negative response 0x{code:02X}")


# --------------------------------------------------------------------------- formulas
def _signed(*parts: float) -> int:
    """Two's complement of the given bytes, most significant first: signed(A), signed(A, B)..."""
    if not 1 <= len(parts) <= 4:
        raise ValueError("signed() takes 1 to 4 bytes")
    value = 0
    for part in parts:
        value = (value << 8) | (int(part) & 0xFF)
    bits = 8 * len(parts)
    return value - (1 << bits) if value >= 1 << (bits - 1) else value


def _bit(value: float, n: float) -> int:
    """Bit n of a byte (0 = least significant)."""
    n = int(n)
    if not 0 <= n <= 31:
        raise ValueError("bit number out of range")
    return (int(value) >> n) & 1


def _shift(op):
    def run(a, b):
        if not 0 <= b <= 32:
            raise ValueError("shift out of range")
        return op(int(a), int(b))
    return run


def _bitwise(op):
    return lambda a, b: op(int(a), int(b))


_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.BitAnd: _bitwise(operator.and_), ast.BitOr: _bitwise(operator.or_), ast.BitXor: _bitwise(operator.xor),
    ast.LShift: _shift(operator.lshift), ast.RShift: _shift(operator.rshift),
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCTIONS = {"signed": _signed, "bit": _bit, "abs": abs, "min": min, "max": max}


class Formula:
    """Arithmetic on the answer's data bytes: ``(A*256+B)/100``, ``signed(A,B)*0.1``, ``bit(C,3)``."""

    MAX_LENGTH = 200

    def __init__(self, text: str):
        self.text = str(text).strip()
        if not self.text or len(self.text) > self.MAX_LENGTH:
            raise PidSetError(f"Invalid formula {text!r}: empty or too long")
        try:
            self._tree = ast.parse(self.text, mode="eval").body
        except (SyntaxError, ValueError) as e:
            raise PidSetError(f"Invalid formula {self.text!r}: {getattr(e, 'msg', e)}") from None
        self.bytes_needed = 0
        self._check(self._tree)

    def _fail(self, what: str):
        raise PidSetError(f"Invalid formula {self.text!r}: {what}")

    def _check(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                self._fail("only numbers are allowed")
        elif isinstance(node, ast.Name):
            if not re.fullmatch(r"[A-Z]{1,2}", node.id):
                self._fail(f"unknown name {node.id!r} (data bytes are A, B, C... Z, AA, AB...)")
            self.bytes_needed = max(self.bytes_needed, byte_index(node.id) + 1)
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BINARY:
                self._fail(f"operator {type(node.op).__name__} is not allowed")
            self._check(node.left)
            self._check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY:
                self._fail(f"operator {type(node.op).__name__} is not allowed")
            self._check(node.operand)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id.lower() not in _FUNCTIONS:
                self._fail("only signed(), bit(), abs(), min() and max() can be called")
            if node.keywords or not node.args or len(node.args) > 4:
                self._fail("wrong function arguments")
            for arg in node.args:
                self._check(arg)
        else:
            self._fail(f"{type(node).__name__} is not allowed")

    def _eval(self, node: ast.AST, data: bytes):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return data[byte_index(node.id)]
        if isinstance(node, ast.BinOp):
            return _BINARY[type(node.op)](self._eval(node.left, data), self._eval(node.right, data))
        if isinstance(node, ast.UnaryOp):
            return _UNARY[type(node.op)](self._eval(node.operand, data))
        return _FUNCTIONS[node.func.id.lower()](*(self._eval(a, data) for a in node.args))  # ast.Call

    def __call__(self, data: bytes) -> Optional[float]:
        """Value for the given data bytes; None when the answer is too short or the result is not a number."""
        if len(data) < self.bytes_needed:
            return None
        try:
            value = float(self._eval(self._tree, data))
        except (ArithmeticError, ValueError, TypeError):
            return None
        return value if math.isfinite(value) else None


# --------------------------------------------------------------------------- definitions
@dataclass
class CustomParam:
    """One manufacturer parameter: what to ask, whom, and how to turn the answer into a value."""
    set_id: str
    name: str                    # key inside the set, e.g. "DPF_SOOT"
    label: str
    request: str                 # "22XXXX", "21XX" or "01XX"
    header: str = DEFAULT_HEADER
    formula: Optional[Formula] = None
    map: dict[int, str] = field(default_factory=dict)   # value -> text, for states
    label_pl: str = ""
    unit: str = ""
    group: str = ""
    decimals: int = 0
    interval: float = 0.0        # seconds between reads; 0 = with every batch
    tile: dict = field(default_factory=dict)             # gauge range and alert limits
    status: str = "unverified"
    note: str = ""
    role: str = ""               # one of ROLES or empty

    @property
    def key(self) -> str:
        """Program-wide key; cannot collide with python-OBD command names."""
        return f"{KEY_PREFIX}{re.sub(r'[^A-Z0-9]', '_', self.set_id.upper())}_{self.name}"

    def param(self) -> Param:
        return Param(key=self.key, label=self.label, unit=self.unit, pid=self.request, group=self.group,
                     decimals=self.decimals, label_pl=self.label_pl, status=self.status, source=self.set_id)

    def decode(self, data: bytes) -> Any:
        """Value from the data bytes that follow the echoed identifier; None when it cannot be decoded."""
        value = self.formula(data) if self.formula else None
        if value is None:
            return None
        if self.map:
            return self.map.get(int(round(value)), str(int(round(value))))
        return round(value, self.decimals)

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "label": self.label, "label_pl": self.label_pl,
                "request": self.request, "header": self.header, "formula": self.formula.text if self.formula else "",
                "map": {str(k): v for k, v in self.map.items()}, "unit": self.unit, "group": self.group,
                "interval": self.interval, "status": self.status, "note": self.note, "role": self.role}


@dataclass
class PidSet:
    id: str
    name: str
    params: list[CustomParam]
    source: str = ""
    license: str = ""            # license of the file when it differs from the program's (e.g. CC BY-SA 4.0)
    note: str = ""
    match: dict = field(default_factory=dict)       # wmi: [...], fuel: [...], years: [from, to], simulator: bool
    verified: list[dict] = field(default_factory=list)
    builtin: bool = False
    path: str = ""

    @property
    def simulator_only(self) -> bool:
        return bool(self.match.get("simulator"))

    def suggested_for(self, vin: Optional[str], year: Optional[int], fuel: Optional[str]) -> bool:
        """True when the file says it was written for a car like this one. Only a hint - sets are
        switched on per car by the user, and probing decides which parameters really answer."""
        if self.simulator_only:
            return False
        wmi, fuels, years = self.match.get("wmi"), self.match.get("fuel"), self.match.get("years")
        if not wmi and not fuels:
            return False
        if wmi and not (vin and any(vin.upper().startswith(prefix) for prefix in wmi)):
            return False
        if fuels and fuel not in fuels:
            return False
        if years and year is not None and not years[0] <= year <= years[1]:
            return False
        return True

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "source": self.source, "license": self.license,
                "note": self.note, "match": self.match,
                "verified": self.verified, "builtin": self.builtin,
                "params": [p.to_dict() for p in self.params]}


# --------------------------------------------------------------------------- parsing
def _text(table: dict, key: str, where: str, required: bool = False, limit: int = 200) -> str:
    value = table.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise PidSetError(f"{where}: '{key}' must be a text of at most {limit} characters")
    if required and not value.strip():
        raise PidSetError(f"{where}: '{key}' is required")
    return value.strip()


def _number(table: dict, key: str, where: str, default=None):
    value = table.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PidSetError(f"{where}: '{key}' must be a number")
    return value


def _parse_param(set_id: str, raw: Any, n: int) -> CustomParam:
    if not isinstance(raw, dict):
        raise PidSetError(f"Parameter {n}: expected a [[params]] table")
    name = _text(raw, "key", f"Parameter {n}", required=True, limit=30).upper()
    where = f"Parameter {name}"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise PidSetError(f"{where}: the key may only contain letters, digits and '_'")
    try:
        request = check_request(_text(raw, "request", where, required=True))
        header = check_header(raw.get("header"))
    except PidSetError as e:
        raise PidSetError(f"{where}: {e}") from None

    mapping: dict[int, str] = {}
    raw_map = raw.get("map", {})
    if not isinstance(raw_map, dict):
        raise PidSetError(f"{where}: 'map' must be a table of value = \"text\"")
    for value, text in raw_map.items():
        if not re.fullmatch(r"-?\d+", str(value)) or not isinstance(text, str) or len(text) > 60:
            raise PidSetError(f"{where}: 'map' must be a table of whole number = \"text\"")
        mapping[int(value)] = text

    formula_text = raw.get("formula", "A" if mapping else "")
    if not isinstance(formula_text, str) or not formula_text.strip():
        raise PidSetError(f"{where}: 'formula' is required (or a 'map' for a state)")
    try:
        formula = Formula(formula_text)
    except PidSetError as e:
        raise PidSetError(f"{where}: {e}") from None

    status = _text(raw, "status", where) or "unverified"
    if status not in STATUSES:
        raise PidSetError(f"{where}: 'status' must be one of {', '.join(STATUSES)}")
    decimals = _number(raw, "decimals", where, 0)
    interval = _number(raw, "interval", where, 0)
    if not isinstance(decimals, int) or not 0 <= decimals <= 6:
        raise PidSetError(f"{where}: 'decimals' must be a whole number from 0 to 6")
    if not 0 <= interval <= 3600:
        raise PidSetError(f"{where}: 'interval' must be between 0 and 3600 seconds")
    tile = {k: _number(raw, k, where) for k in TILE_FIELDS if raw.get(k) is not None}
    role = _text(raw, "role", where)
    if role and role not in ROLES:
        raise PidSetError(f"{where}: 'role' must be one of {', '.join(ROLES)}")

    return CustomParam(
        set_id=set_id, name=name, label=_text(raw, "label", where, required=True, limit=80), request=request,
        header=header, formula=formula, map=mapping, label_pl=_text(raw, "label_pl", where, limit=80),
        unit="" if mapping else _text(raw, "unit", where, limit=16), group=_text(raw, "group", where, limit=40),
        decimals=decimals, interval=float(interval), tile=tile, status=status, note=_text(raw, "note", where, limit=400),
        role=role)


def _parse_match(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise PidSetError("'match' must be a table")
    match: dict = {}
    for key in ("wmi", "fuel"):
        value = raw.get(key)
        if value is None:
            continue
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(i, str) and 0 < len(i) <= 17 for i in items):
            raise PidSetError(f"match: '{key}' must be a text or a list of texts")
        match[key] = [i.upper() if key == "wmi" else i.lower() for i in items]
    years = raw.get("years")
    if years is not None:
        if (not isinstance(years, list) or len(years) != 2
                or not all(isinstance(y, int) and not isinstance(y, bool) for y in years) or years[0] > years[1]):
            raise PidSetError("match: 'years' must be [first, last]")
        match["years"] = years
    if raw.get("simulator") is True:
        match["simulator"] = True
    return match


def parse_set(text: str, builtin: bool = False, path: str = "") -> PidSet:
    """Parse and validate one PID set file. Raises PidSetError with a message meant for the user."""
    if len(text.encode("utf-8", errors="replace")) > MAX_FILE_SIZE:
        raise PidSetError("The file is too large")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PidSetError(f"Not a valid TOML file: {e}") from None
    if data.get("format") != FORMAT:
        raise PidSetError(f"Unsupported file format (expected format = {FORMAT})")
    set_id = _text(data, "id", "Set", required=True, limit=40)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", set_id):
        raise PidSetError("Set: 'id' may only contain lower case letters, digits and '-'")
    raw_params = data.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        raise PidSetError("The set has no [[params]]")
    if len(raw_params) > MAX_PARAMS:
        raise PidSetError(f"Too many parameters (the limit is {MAX_PARAMS})")
    params = [_parse_param(set_id, raw, n) for n, raw in enumerate(raw_params, start=1)]
    names = [p.name for p in params]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise PidSetError(f"Duplicate parameter key(s): {', '.join(duplicates)}")
    name = _text(data, "name", "Set", required=True, limit=80)
    for p in params:
        p.group = p.group or name
    verified = data.get("verified", [])
    if not isinstance(verified, list) or not all(isinstance(v, dict) for v in verified):
        raise PidSetError("'verified' must be a list of [[verified]] tables")
    return PidSet(id=set_id, name=name, params=params, source=_text(data, "source", "Set", limit=400),
                  license=_text(data, "license", "Set", limit=80),
                  note=_text(data, "note", "Set", limit=600), match=_parse_match(data.get("match", {})),
                  verified=[{k: str(v)[:120] for k, v in entry.items()} for entry in verified],
                  builtin=builtin, path=path)


def load_sets(user_dir: Optional[Path] = None) -> tuple[list[PidSet], list[dict]]:
    """All usable sets (a user's file replaces a built-in one with the same id) and the files that failed."""
    sets: dict[str, PidSet] = {}
    errors: list[dict] = []
    for folder, builtin in ((BUILTIN_DIR, True), (user_dir, False)):
        if folder is None or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.toml")):
            try:
                found = parse_set(path.read_text(encoding="utf-8"), builtin=builtin, path=str(path))
                sets[found.id] = found
            except (PidSetError, OSError, UnicodeDecodeError) as e:
                errors.append({"file": path.name, "error": str(e)})
    return sorted(sets.values(), key=lambda s: (not s.builtin, s.name.lower())), errors


# --------------------------------------------------------------------------- per car
def enabled_sets(sets: list[PidSet], vehicle: dict, simulated: bool) -> list[PidSet]:
    """Sets switched on for this car: the user's choice in the profile; the simulator gets its demo set."""
    chosen = set(vehicle.get("pidsets") or [])
    return [s for s in sets
            if (s.simulator_only and simulated) or (not s.simulator_only and s.id in chosen)]


def apply(session, storage, vehicle: dict, reprobe: bool = False) -> dict:
    """Switch the vehicle's sets on for this session.

    Every parameter is asked for once ("probing"); what the car answered is remembered in the
    profile, so later connections only read the parameters that exist in this car.
    Returns the probe results: parameter key -> "ok", "none" (no answer), "bad" (answer too short
    for the formula) or "nrcXX" (negative response).
    """
    sets, _ = load_sets(storage.pidsets_dir)
    params = [p for s in enabled_sets(sets, vehicle, session.simulated) for p in s.params]
    known = {} if reprobe else dict(vehicle.get("pid_probe") or {})
    results = {p.key: known[p.key] for p in params if p.key in known}
    missing = [p for p in params if p.key not in results]
    if missing:
        results.update(session.probe_custom(missing))
    if results != (vehicle.get("pid_probe") or {}):
        storage.update(vehicle["id"], pid_probe=results)
    session.set_custom_params([p for p in params if results.get(p.key) == "ok"])
    return results
