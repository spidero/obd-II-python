"""Plain data objects returned by the core layer (JSON-friendly)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Reading:
    key: str            # python-OBD command name, e.g. "RPM"
    label: str          # human readable label
    value: Any          # float, int, str or None when no data
    unit: str = ""
    pid: str = ""       # e.g. "010C"
    time: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Param:
    key: str
    label: str
    unit: str
    pid: str
    group: str
    decimals: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dtc:
    code: str
    description: str
    kind: str  # "stored" | "pending"
    system: str = ""  # name of the system the code belongs to

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DtcReport:
    stored: list[Dtc] = field(default_factory=list)
    pending: list[Dtc] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.stored) + len(self.pending)

    def to_dict(self) -> dict:
        return {
            "stored": [d.to_dict() for d in self.stored],
            "pending": [d.to_dict() for d in self.pending],
        }


@dataclass
class ReadinessTest:
    key: str
    label: str
    available: bool
    complete: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VehicleStatus:
    mil: bool
    dtc_count: int
    ignition_type: str
    readiness: list[ReadinessTest] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mil": self.mil,
            "dtc_count": self.dtc_count,
            "ignition_type": self.ignition_type,
            "readiness": [t.to_dict() for t in self.readiness],
        }


@dataclass
class AdapterInfo:
    port: str
    elm_version: str | None
    voltage: float | None
    protocol: str | None
    protocol_id: str | None
    simulated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
