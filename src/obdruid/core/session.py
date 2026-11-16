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
from . import custom_pids, diag, transports, vehicle_id
from .custom_pids import CustomParam, PidSetError
from .models import (AdapterInfo, Dtc, DtcReport, FreezeFrame, MonitorReport, Param, Reading, ReadinessTest,
                     VehicleStatus)
from .pids import NOT_LIVE, READINESS_LABELS, param_for, unit_name

log = logging.getLogger(__name__)

# Keep python-OBD quiet unless asked otherwise.
obd.logger.setLevel(logging.WARNING)


class ObdError(RuntimeError):
    """Raised when the adapter or the car does not respond as expected."""


@dataclass
class DidAnswer:
    """Answer to one manufacturer request (service 22, or a mode 01 PID python-OBD does not know)."""
    request: str                 # e.g. "22F190"
    header: str                  # request address, e.g. "7E0"
    data: bytes | None = None    # data bytes after the echoed identifier; None = no positive answer
    nrc: int | None = None       # negative response code, when the module refused

    @property
    def ok(self) -> bool:
        return self.data is not None

    @property
    def result(self) -> str:
        """Short outcome stored in the vehicle profile: "ok", "none" or "nrcXX"."""
        if self.ok:
            return "ok"
        return "none" if self.nrc is None else f"nrc{self.nrc:02X}"


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
        self._header: str | None = None                 # request address set with AT SH; None = adapter default
        self._custom: dict[str, CustomParam] = {}       # manufacturer parameters switched on for this car
        self._custom_cache: dict[str, tuple[float, Reading]] = {}

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
            baudrate, timeout = s.baudrate, s.timeout
            if transports.is_url(port):
                transports.register()
                baudrate = baudrate or 38400       # no baud rate detection over a wireless link
                timeout = max(timeout, 2.0)        # Bluetooth/Wi-Fi adds latency
            log.info("Connecting to %s", port)
            conn = obd.OBD(portstr=port, baudrate=baudrate, protocol=s.protocol,
                           fast=s.fast, timeout=timeout)
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
            self._complete_pid_masks(conn)
            self._conn = conn
            self._header = None       # the adapter was reset: it sends to the functional address again
            self._custom_cache.clear()
            self.last_error = None

    @staticmethod
    def _complete_pid_masks(conn, tries: int = 3) -> None:
        """Re-ask "supported PIDs" masks whose answer got lost (python-OBD then silently drops that whole
        range – over Bluetooth this hides e.g. the control module voltage). A mask is missing when the
        previous mask announced it but none of its 32 PIDs is known."""
        for getter in obd.commands.pid_getters():
            if getter.mode != 1 or not conn.supports(getter):
                continue
            first, last = getter.pid + 1, getter.pid + 0x20
            if any(c.mode == 1 and first <= c.pid <= last for c in conn.supported_commands):
                continue
            for _ in range(tries):
                response = conn.query(getter)
                if not response.is_null():
                    for i, bit in enumerate(response.value):
                        if bit and obd.commands.has_pid(1, getter.pid + i + 1):
                            conn.supported_commands.add(obd.commands[1][getter.pid + i + 1])
                        if bit and obd.commands.has_pid(2, getter.pid + i + 1):
                            conn.supported_commands.add(obd.commands[2][getter.pid + i + 1])
                    log.info("Recovered the PID mask %s on retry", getter.name)
                    break

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
    # Functional (broadcast) request address on 11 bit CAN - what the adapter uses until AT SH changes it.
    FUNCTIONAL_HEADER = "7DF"

    def _set_header(self, header: str | None) -> None:
        """Address the next requests to one module (physical addressing) or, with None, to all of them.

        python-OBD is never told about the header (its commands keep their default one), so its standard
        queries work unchanged; `_query` restores the functional address before each of them.
        The AT command goes through `conn.query`, not straight to the adapter: python-OBD sends a bare
        carriage return ("repeat") when a request equals the previous one it knows about, and the adapter
        would then repeat the AT SH instead of the request.
        """
        if header == self._header:
            return
        conn = self._require()
        target = header or self.FUNCTIONAL_HEADER
        cmd = obd.OBDCommand("SET_HEADER", "set the request address", b"ATSH" + target.encode(), 0,
                             lambda messages: " ".join(m.raw() for m in messages), obd.ECU.ALL, False)
        if "OK" not in (conn.query(cmd, force=True).value or ""):
            self._header = "unknown"   # forces a new AT SH before the next request
            raise ObdError(f"The adapter did not accept the request address {target}")
        self._header = header

    def _query(self, cmd, force: bool = False, header: str | None = None):
        conn = self._require()
        self._set_header(header)
        response = conn.query(cmd, force=force)
        if conn.status() == OBDStatus.NOT_CONNECTED:
            self.last_error = "Lost connection to the adapter"
            raise ObdError(self.last_error)
        return response

    def _raw(self, command: str, header: str | None = None) -> list[bytes]:
        """Send a raw service request, return the response payloads (engine ECU first)."""
        cmd = obd.OBDCommand("RAW_" + command, "raw " + command, command.encode(), 0,
                             lambda messages: [(m.ecu, bytes(m.data)) for m in messages], obd.ECU.ALL, False)
        answers = self._query(cmd, force=True, header=header).value or []
        answers.sort(key=lambda item: item[0] != obd.ECU.ENGINE)
        return [data for _, data in answers]

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

    def adapter_voltage(self) -> float | None:
        """Battery voltage measured by the adapter at the OBD socket (AT RV) – fast, works with the engine off."""
        with self._lock:
            self._require()
            value = self._query(obd.commands.ELM_VOLTAGE).value
            return round(float(value.magnitude), 2) if value is not None else None

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

    def identify(self) -> tuple[str | None, dict | None]:
        """(VIN, fingerprint): the fingerprint is only worked out when there is no VIN to go by."""
        vin = self.vin()
        return vin, (None if vin else self.fingerprint())

    def fingerprint(self) -> dict | None:
        """What tells this car apart when it reports no VIN: protocol, the modules that answer and the
        standard PIDs it supports. Stable between connections; compare with `vehicle_id.same_car`."""
        with self._lock:
            conn = self._require()
            pids = sorted({c.pid for c in conn.supported_commands if c.mode == 1 and c.pid})
            cmd = obd.OBDCommand("RAW_0100_ECUS", "modules answering 0100", b"0100", 0,
                                 lambda messages: sorted({f"{m.frames[0].tx_id:02X}" for m in messages
                                                          if m.frames and m.frames[0].tx_id is not None}),
                                 obd.ECU.ALL, False)
            try:
                ecus = self._query(cmd, force=True).value or []
            except (TypeError, ValueError, AttributeError):  # an odd answer must not stop the connection
                ecus = []
            return vehicle_id.make_fingerprint(conn.protocol_id(), ecus, pids)

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
            return [param_for(c) for c in cmds] + [p.param() for p in self._custom.values()]

    # ------------------------------------------------------------------ manufacturer parameters
    def read_did(self, request: str, header: str = custom_pids.DEFAULT_HEADER) -> DidAnswer:
        """One read-only manufacturer request (service 22 or 01), addressed to a single module."""
        try:
            request, header = custom_pids.check_request(request), custom_pids.check_header(header)
        except PidSetError as e:
            raise ObdError(str(e)) from None
        with self._lock:
            conn = self._require()
            if conn.protocol_id() not in ("6", "8"):
                raise ObdError("Manufacturer parameters need a car with 11 bit CAN "
                               f"(this one uses {conn.protocol_name()})")
            payloads = self._raw(request, header=header)
            echo = bytes([int(request[:2], 16) + 0x40]) + bytes.fromhex(request[2:])
            answer = DidAnswer(request=request, header=header)
            for data in payloads:
                if data[:len(echo)] == echo:
                    answer.data = data[len(echo):]
                    return answer
                # 7F <service> <code>; 0x78 only says "wait, the answer is coming"
                if len(data) >= 3 and data[0] == 0x7F and data[1] == echo[0] - 0x40 and data[2] != 0x78:
                    answer.nrc = data[2]
            return answer

    def set_custom_params(self, params: list[CustomParam]) -> None:
        """Manufacturer parameters to offer next to the standard ones (see custom_pids.apply)."""
        with self._lock:
            self._custom = {p.key: p for p in params}
            self._custom_cache.clear()

    def custom_params(self) -> list[CustomParam]:
        with self._lock:
            return list(self._custom.values())

    def probe_custom(self, params: list[CustomParam]) -> dict[str, str]:
        """Ask once for every parameter: key -> "ok", "none", "bad" or "nrcXX"."""
        with self._lock:
            answers: dict[tuple[str, str], DidAnswer] = {}
            results = {}
            for p in sorted(params, key=lambda p: p.header):
                try:
                    answer = answers.get((p.header, p.request)) or self.read_did(p.request, p.header)
                except ObdError:
                    if not self.connected:
                        raise
                    results[p.key] = "none"   # e.g. the car is not on 11 bit CAN
                    continue
                answers[(p.header, p.request)] = answer
                results[p.key] = "bad" if answer.ok and p.decode(answer.data) is None else answer.result
            return results

    def _read_custom(self, keys: list[str]) -> dict[str, Reading]:
        """Read manufacturer parameters: one request per identifier, grouped by module, slow ones from cache."""
        now = time.monotonic()
        answers: dict[tuple[str, str], DidAnswer] = {}
        out = {}
        for p in sorted((self._custom[k] for k in keys), key=lambda p: p.header):
            cached = self._custom_cache.get(p.key)
            if cached and p.interval and now - cached[0] < p.interval:
                out[p.key] = cached[1]
                continue
            answer = answers.get((p.header, p.request))
            if answer is None:
                answer = answers[(p.header, p.request)] = self.read_did(p.request, p.header)
            reading = Reading(key=p.key, label=p.label, value=p.decode(answer.data) if answer.ok else None,
                              unit=p.unit, pid=p.request, time=time.time())
            self._custom_cache[p.key] = (now, reading)
            out[p.key] = reading
        return out

    def read(self, keys: list[str]) -> list[Reading]:
        """Read current values of the given parameters (python-OBD command names)."""
        with self._lock:
            conn = self._require()
            readings = []
            custom = self._read_custom([k for k in keys if k in self._custom])
            for key in keys:
                if key in custom:
                    readings.append(custom[key])
                    continue
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
            permanent = diag.read_permanent(self._raw)
            report.permanent_supported = permanent is not None
            report.permanent = permanent or []
            return report

    def read_freeze_frame(self) -> FreezeFrame:
        """Engine conditions stored with the fault (mode 02, frame 0)."""
        with self._lock:
            return diag.read_freeze_frame(self._raw, _plain_value)

    def read_monitors(self) -> MonitorReport:
        """On-board monitor test results (mode 06). Only available on CAN."""
        with self._lock:
            conn = self._require()
            if conn.protocol_id() not in ("6", "7", "8", "9"):
                return MonitorReport(supported=False)
            return diag.read_monitors(self._raw)

    def clear_dtcs(self) -> None:
        """Clear trouble codes (mode 04). Also resets readiness monitors and freeze frames."""
        with self._lock:
            conn = self._require()
            self._set_header(None)
            response = conn.query(obd.commands.CLEAR_DTC, force=True)
            # a positive answer is service id 0x44 (0x04 + 0x40)
            if not any(m.data[:1] == bytearray([0x44]) for m in response.messages):
                raw = " | ".join(m.raw() for m in response.messages) or "no response"
                raise ObdError("The ECU did not confirm clearing the codes "
                               f"({raw}). Some cars require the engine off and the ignition on.")
