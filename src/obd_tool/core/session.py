"""Thread-safe OBD-II session on top of python-OBD.

One ObdSession owns the serial port. All public methods are serialized with
a lock, so the session can be shared by the web server and background pollers.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import obd
from obd import OBDStatus

from . import dtc as dtc_codes
from .models import AdapterInfo, Dtc, DtcReport, Param, Reading, ReadinessTest, VehicleStatus
from .pids import NOT_LIVE, READINESS_LABELS, param_for, unit_name

log = logging.getLogger(__name__)

# Keep python-OBD quiet unless asked otherwise.
obd.logger.setLevel(logging.WARNING)


class ObdError(RuntimeError):
    """Raised when the adapter or the car does not respond as expected."""


@dataclass
class ConnectionSettings:
    port: str | None = None          # None = auto detect
    baudrate: int | None = None      # None = auto detect
    protocol: str | None = None      # None = auto (ATSP0)
    fast: bool = True
    timeout: float = 1.0             # used for baud rate detection
    simulate: bool = False
    sim_faults: tuple[str, ...] = ()  # see simulator.FAULTS
    sim_codes: bool = True            # simulator starts with sample trouble codes


def find_ports() -> list[str]:
    """Serial ports that look like OBD adapters, stable /dev/serial/by-id paths first."""
    import glob

    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    ports = list(by_id)
    try:
        from serial.tools import list_ports

        for p in list_ports.comports():
            if p.device not in ports:
                ports.append(p.device)
    except Exception:  # pragma: no cover - list_ports is optional on some platforms
        pass
    return ports


def _plain_value(value):
    """Convert python-OBD values (pint quantities, tuples, enums) to JSON-friendly data."""
    if value is None:
        return None, ""
    if hasattr(value, "magnitude") and hasattr(value, "units"):
        return float(value.magnitude), unit_name(str(value.units))
    if isinstance(value, (tuple, list)):
        if value and hasattr(value[0], "magnitude"):
            return _plain_value(value[0])  # e.g. O2 sensor: (voltage, trim)
        parts = [str(v).strip() for v in value if v not in (None, "")]
        return ", ".join(p for p in parts if p), ""
    if isinstance(value, str):
        return value.strip(), ""
    if isinstance(value, (int, float, bool)):
        return value, ""
    return str(value), ""


class ObdSession:
    def __init__(self, settings: ConnectionSettings | None = None):
        self.settings = settings or ConnectionSettings()
        self._lock = threading.RLock()
        self._conn: obd.OBD | None = None
        self._emulator = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        with self._lock:
            self.close()
            s = self.settings
            port = s.port
            if s.simulate:
                from .simulator import Elm327Emulator

                kwargs = {} if s.sim_codes else {"stored_dtcs": (), "pending_dtcs": ()}
                self._emulator = Elm327Emulator(faults=s.sim_faults, **kwargs).start()
                port = self._emulator.port
            elif port is None:
                ports = find_ports()
                if not ports:
                    raise ObdError("No OBD adapter found (no serial ports)")
                port = ports[0]
            log.info("Connecting to %s", port)
            conn = obd.OBD(portstr=port, baudrate=s.baudrate, protocol=s.protocol,
                           fast=s.fast, timeout=s.timeout)
            status = conn.status()
            if status == OBDStatus.NOT_CONNECTED:
                conn.close()
                self._stop_emulator()
                self.last_error = f"No response from the adapter on {port}"
                raise ObdError(self.last_error)
            if status != OBDStatus.CAR_CONNECTED:
                conn.close()
                self._stop_emulator()
                self.last_error = ("The adapter responds but the car does not (state: "
                                   f"{status}). Is the ignition on?")
                raise ObdError(self.last_error)
            self._conn = conn
            self.last_error = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
            self._stop_emulator()

    def _stop_emulator(self) -> None:
        if self._emulator is not None:
            self._emulator.stop()
            self._emulator = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._conn.status() == OBDStatus.CAR_CONNECTED

    @property
    def simulated(self) -> bool:
        return self._emulator is not None

    def _require(self) -> obd.OBD:
        if not self.connected:
            raise ObdError("Not connected to the car")
        return self._conn  # type: ignore[return-value]

    def __enter__(self):
        if not self.connected:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ queries
    def _query(self, cmd, force: bool = False):
        conn = self._require()
        response = conn.query(cmd, force=force)
        if conn.status() == OBDStatus.NOT_CONNECTED:
            self.last_error = "Lost connection to the adapter"
            raise ObdError(self.last_error)
        return response

    def adapter_info(self) -> AdapterInfo:
        with self._lock:
            conn = self._require()
            version = self._query(obd.commands.ELM_VERSION).value
            voltage = self._query(obd.commands.ELM_VOLTAGE).value
            return AdapterInfo(
                port=conn.port_name(),
                elm_version=str(version).strip() if version else None,
                voltage=round(float(voltage.magnitude), 2) if voltage is not None else None,
                protocol=conn.protocol_name(),
                protocol_id=conn.protocol_id(),
                simulated=self.simulated,
            )

    def vin(self) -> str | None:
        """Vehicle identification number, or None when the car does not report it."""
        with self._lock:
            value = self._query(obd.commands.VIN, force=True).value
            if value is None:
                return None
            if isinstance(value, (bytes, bytearray)):
                value = value.decode(errors="ignore")
            vin = str(value).strip().strip("\x00").strip()
            return vin or None

    def status(self) -> VehicleStatus:
        with self._lock:
            value = self._query(obd.commands.STATUS).value
            if value is None:
                raise ObdError("The car did not return its status (PID 0101)")
            tests = []
            for key, label in READINESS_LABELS.items():
                test = getattr(value, key, None)
                if test is None or not test.available:
                    continue
                tests.append(ReadinessTest(key=key, label=label, available=True, complete=bool(test.complete)))
            ignition = {"spark": "spark (petrol)", "compression": "compression (diesel)"}.get(
                value.ignition_type, value.ignition_type)
            return VehicleStatus(mil=bool(value.MIL), dtc_count=int(value.DTC_count),
                                 ignition_type=ignition, readiness=tests)

    def supported_params(self) -> list[Param]:
        """Live (mode 01) parameters supported by the car, in PID order."""
        with self._lock:
            conn = self._require()
            cmds = [c for c in conn.supported_commands
                    if c.mode == 1 and c.name not in NOT_LIVE]
            cmds.sort(key=lambda c: c.pid)
            return [param_for(c) for c in cmds]

    def read(self, keys: list[str]) -> list[Reading]:
        """Read current values of the given parameters (python-OBD command names)."""
        with self._lock:
            conn = self._require()
            readings = []
            for key in keys:
                if key not in obd.commands:
                    raise ObdError(f"Unknown parameter: {key}")
                cmd = obd.commands[key]
                param = param_for(cmd)
                if not conn.supports(cmd):
                    readings.append(Reading(key=key, label=param.label, value=None, unit=param.unit,
                                            pid=param.pid, time=time.time()))
                    continue
                response = self._query(cmd)
                value, unit = _plain_value(response.value)
                if isinstance(value, float):
                    value = round(value, param.decimals)
                readings.append(Reading(key=key, label=param.label, value=value,
                                        unit=param.unit or unit, pid=param.pid, time=response.time))
            return readings

    def read_dtcs(self) -> DtcReport:
        with self._lock:
            report = DtcReport()
            for cmd, kind, target in ((obd.commands.GET_DTC, "stored", report.stored),
                                      (obd.commands.GET_CURRENT_DTC, "pending", report.pending)):
                value = self._query(cmd, force=True).value or []
                for code, description in value:
                    description = dtc_codes.clean_description(description) or dtc_codes.describe(code)
                    target.append(Dtc(code=code, description=description,
                                      kind=kind, system=dtc_codes.system_of(code)))
            return report

    def clear_dtcs(self) -> None:
        """Clear trouble codes (mode 04). Also resets readiness monitors and freeze frames."""
        with self._lock:
            conn = self._require()
            response = conn.query(obd.commands.CLEAR_DTC, force=True)
            # a positive answer is service id 0x44 (0x04 + 0x40)
            if not any(m.data[:1] == bytearray([0x44]) for m in response.messages):
                raw = " | ".join(m.raw() for m in response.messages) or "no response"
                raise ObdError("The ECU did not confirm clearing the codes "
                               f"({raw}). Some cars require the engine off and the ignition on.")
