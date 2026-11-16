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
    label_pl: str = ""   # Polish label of a parameter that comes from a PID set file
    status: str = ""     # "verified" / "unverified" for manufacturer parameters, "" for standard ones
    source: str = ""     # id of the PID set the parameter comes from

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Dtc:
    code: str
    description: str
    kind: str  # "stored" | "pending" | "permanent" | "freeze"
    system: str = ""  # name of the system the code belongs to

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DtcReport:
    stored: list[Dtc] = field(default_factory=list)
    pending: list[Dtc] = field(default_factory=list)
    permanent: list[Dtc] = field(default_factory=list)   # mode 0A: survive clearing until the fault is proven fixed
    permanent_supported: bool = False

    @property
    def total(self) -> int:
        """Stored + pending codes (permanent codes repeat stored ones and cannot be cleared)."""
        return len(self.stored) + len(self.pending)

    def to_dict(self) -> dict:
        return {
            "stored": [d.to_dict() for d in self.stored],
            "pending": [d.to_dict() for d in self.pending],
            "permanent": [d.to_dict() for d in self.permanent],
            "permanent_supported": self.permanent_supported,
        }


@dataclass
class FreezeFrame:
    """Engine conditions recorded by the ECU when a fault was detected (mode 02)."""
    supported: bool = False          # the ECU answers mode 02 requests
    dtc: Dtc | None = None           # code that triggered the frame; None = no frame stored
    readings: list[Reading] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.dtc is not None

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "present": self.present,
            "dtc": self.dtc.to_dict() if self.dtc else None,
            "readings": [r.to_dict() for r in self.readings],
        }


@dataclass
class MonitorTest:
    """One on-board monitor test result (mode 06): measured value against the ECU's limits."""
    mid: int                 # monitor id, e.g. 0x21 = catalyst bank 1
    tid: int                 # test id (0x80+ are manufacturer defined)
    monitor: str             # monitor name
    name: str                # test name
    value: float
    min: float | None        # None = no lower limit
    max: float | None        # None = no upper limit
    unit: str = ""
    status: str = "pass"     # pass | near | fail
    margin: float | None = None   # headroom to the nearest limit in % (0 = at the limit)

    @property
    def id(self) -> str:
        return f"{self.mid:02X}:{self.tid:02X}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["id"] = self.id
        return data


@dataclass
class MonitorReport:
    supported: bool = False
    tests: list[MonitorTest] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for t in self.tests if t.status == status)

    def to_dict(self) -> dict:
        return {
            "supported": self.supported,
            "tests": [t.to_dict() for t in self.tests],
            "counts": {k: self.count(k) for k in ("pass", "near", "fail")},
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
