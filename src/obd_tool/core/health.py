"""Vehicle health check: sample live data for a while, then score it with simple rules.

collect() talks to the car; evaluate() is a pure function on the collected Snapshot,
so the rules can be unit tested with synthetic data.

The thresholds are indicative workshop rules of thumb for petrol engines, not
manufacturer specifications. The report says so.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .diag import MISFIRE_MIDS
from .models import DtcReport, FreezeFrame, MonitorReport, VehicleStatus

# Keys sampled during the whole check
SAMPLE_KEYS = [
    "RPM", "SPEED", "COOLANT_TEMP", "ENGINE_LOAD", "INTAKE_PRESSURE",
    "SHORT_FUEL_TRIM_1", "LONG_FUEL_TRIM_1", "SHORT_FUEL_TRIM_2", "LONG_FUEL_TRIM_2",
    "CONTROL_MODULE_VOLTAGE", "O2_B1S1", "O2_B1S2",
]
# Keys sampled fast in the last phase (O2 sensor switching needs a higher rate)
FAST_KEYS = ["O2_B1S1", "O2_B1S2"]
# Read once
COUNTER_KEYS = [
    "RUN_TIME", "DISTANCE_W_MIL", "DISTANCE_SINCE_DTC_CLEAR", "WARMUPS_SINCE_DTC_CLEAR",
    "TIME_SINCE_DTC_CLEARED", "BAROMETRIC_PRESSURE",
]

SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2, "ok": 3, "skipped": 4}

DISCLAIMER = ("This is an indicative check based on generic OBD-II data and rules of thumb. "
              "It does not replace a full workshop diagnosis.")


@dataclass
class Snapshot:
    status: VehicleStatus
    dtcs: DtcReport
    counters: dict[str, float]
    samples: dict[str, list[tuple[float, float]]]  # key -> [(t, value)]
    duration: float
    adapter_voltage: Optional[float] = None
    vehicle: dict = field(default_factory=dict)
    freeze: Optional[FreezeFrame] = None       # None = not read
    monitors: Optional[MonitorReport] = None   # None = not read

    def values(self, key: str) -> list[float]:
        return [v for _, v in self.samples.get(key, []) if isinstance(v, (int, float))]


@dataclass
class Finding:
    key: str
    area: str
    severity: str  # critical | warn | info | ok | skipped
    title: str
    detail: str = ""
    value: str = ""
    penalty: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthReport:
    score: int
    grade: str
    findings: list[Finding]
    created: str
    duration: float
    samples: int
    vehicle: dict = field(default_factory=dict)
    disclaimer: str = DISCLAIMER
    freeze_frame: Optional[dict] = None   # FreezeFrame.to_dict() when a frame is stored
    monitor_tests: list[dict] = field(default_factory=list)   # MonitorTest.to_dict()

    @property
    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in SEVERITY_ORDER}
        for f in self.findings:
            out[f.severity] += 1
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "HealthReport":
        """Rebuild a report saved with to_dict() (history records)."""
        fields = ("key", "area", "severity", "title", "detail", "value", "penalty")
        findings = [Finding(**{k: f[k] for k in fields if k in f}) for f in data.get("findings", [])]
        return cls(score=data["score"], grade=data["grade"], findings=findings, created=data["created"],
                   duration=data.get("duration", 0.0), samples=data.get("samples", 0),
                   vehicle=data.get("vehicle") or {}, disclaimer=data.get("disclaimer", DISCLAIMER),
                   freeze_frame=data.get("freeze_frame"), monitor_tests=data.get("monitor_tests") or [])

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "counts": self.counts,
            "findings": [f.to_dict() for f in self.findings],
            "created": self.created,
            "duration": round(self.duration, 1),
            "samples": self.samples,
            "vehicle": self.vehicle,
            "disclaimer": self.disclaimer,
            "freeze_frame": self.freeze_frame,
            "monitor_tests": self.monitor_tests,
        }


ProgressFn = Callable[[float, str], None]


# --------------------------------------------------------------------------- collection
def collect(session, duration: float = 30.0, progress: Optional[ProgressFn] = None) -> Snapshot:
    """Read codes, monitors and counters, then sample live data for `duration` seconds."""
    def report(fraction: float, message: str) -> None:
        if progress:
            progress(min(1.0, max(0.0, fraction)), message)

    report(0.0, "Reading status and trouble codes")
    status = session.status()
    dtcs = session.read_dtcs()
    supported = {p.key for p in session.supported_params()}

    vehicle = {}
    try:
        adapter = session.adapter_info()
        vehicle = {"vin": session.vin(), "protocol": adapter.protocol, "adapter": adapter.elm_version,
                   "port": adapter.port, "simulated": adapter.simulated}
        adapter_voltage = adapter.voltage
    except Exception:  # informational only
        adapter_voltage = None

    report(0.02, "Reading freeze frame and monitor tests")
    freeze = monitors = None
    try:
        freeze = session.read_freeze_frame()
        monitors = session.read_monitors()
    except Exception:  # optional services: an ECU choking on them must not stop the check
        pass

    counters = {}
    keys = [k for k in COUNTER_KEYS if k in supported]
    for r in session.read(keys) if keys else []:
        if isinstance(r.value, (int, float)):
            counters[r.key] = float(r.value)

    sample_keys = [k for k in SAMPLE_KEYS if k in supported]
    fast_keys = [k for k in FAST_KEYS if k in supported]
    samples: dict[str, list[tuple[float, float]]] = {k: [] for k in sample_keys}

    start = time.monotonic()
    slow_phase = duration * (2 / 3 if fast_keys else 1.0)
    while (elapsed := time.monotonic() - start) < duration:
        tick = time.monotonic()
        phase_keys = sample_keys if elapsed < slow_phase else fast_keys
        label = "Sampling live data" if elapsed < slow_phase else "Sampling O2 sensors"
        report(0.05 + 0.95 * elapsed / duration, label)
        for r in session.read(phase_keys):
            if isinstance(r.value, (int, float)) and not isinstance(r.value, bool):
                samples[r.key].append((time.monotonic() - start, float(r.value)))
        # at most ~50 batches/s: a real car is slower anyway, the simulator would spin the CPU
        time.sleep(max(0.0, 0.02 - (time.monotonic() - tick)))
    report(1.0, "Evaluating")
    return Snapshot(status=status, dtcs=dtcs, counters=counters, samples=samples,
                    duration=time.monotonic() - start, adapter_voltage=adapter_voltage, vehicle=vehicle,
                    freeze=freeze, monitors=monitors)


# --------------------------------------------------------------------------- helpers
def _crossings(values: list[float], low: float, high: float) -> int:
    """Count rich/lean transitions with hysteresis (narrow-band O2 sensor)."""
    state = None
    count = 0
    for v in values:
        new = "rich" if v >= high else "lean" if v <= low else state
        if state is not None and new != state:
            count += 1
        state = new
    return count


def _fmt(v: float, unit: str = "", decimals: int = 1) -> str:
    return f"{v:.{decimals}f}{(' ' + unit) if unit else ''}"


# --------------------------------------------------------------------------- rules
def _rule_dtcs(s: Snapshot) -> list[Finding]:
    out = []
    stored, pending = s.dtcs.stored, s.dtcs.pending
    if s.status.mil:
        out.append(Finding("mil", "Trouble codes", "critical", "Check engine light (MIL) is on",
                           "The ECU has confirmed an emissions related fault.", "ON", 10))
    for d in stored:
        out.append(Finding(f"dtc_{d.code}", "Trouble codes", "critical", f"Stored code {d.code}",
                           d.description or "Manufacturer specific code", d.system, 15))
    for d in pending:
        out.append(Finding(f"dtc_{d.code}", "Trouble codes", "warn", f"Pending code {d.code}",
                           (d.description or "Manufacturer specific code")
                           + " — detected once, not confirmed yet.", d.system, 5))
    stored_codes = {d.code for d in stored}
    for d in s.dtcs.permanent:
        if d.code in stored_codes:
            continue  # already reported as a stored code
        out.append(Finding(f"permanent_{d.code}", "Trouble codes", "warn", f"Permanent code {d.code}",
                           (d.description or "Manufacturer specific code")
                           + " — the codes were cleared, but the ECU has not yet confirmed that this fault "
                           "is gone. It disappears by itself after the monitor passes.", d.system, 5))
    if not stored and not pending and not s.status.mil:
        out.append(Finding("dtc", "Trouble codes", "ok", "No trouble codes",
                           "No stored or pending codes, MIL off."))
    if s.freeze is not None and s.freeze.present:
        shown = {r.key: r for r in s.freeze.readings}
        parts = [f"{shown[k].value:g} {shown[k].unit}" for k in ("RPM", "SPEED", "COOLANT_TEMP", "ENGINE_LOAD")
                 if k in shown and isinstance(shown[k].value, (int, float))]
        out.append(Finding("freeze_frame", "Trouble codes", "info", f"Freeze frame stored for {s.freeze.dtc.code}",
                           "Engine conditions when the fault was detected"
                           + (": " + ", ".join(parts) if parts else "") + ". See the freeze frame table.",
                           s.freeze.dtc.code))
    dist_mil = s.counters.get("DISTANCE_W_MIL")
    if dist_mil:
        out.append(Finding("distance_mil", "Trouble codes", "warn" if dist_mil > 100 else "info",
                           "Distance driven with MIL on", "The car was driven with the check engine light on.",
                           _fmt(dist_mil, "km", 0), 5 if dist_mil > 100 else 0))
    return out


def _rule_readiness(s: Snapshot) -> list[Finding]:
    tests = s.status.readiness
    if not tests:
        return [Finding("readiness", "Readiness", "skipped", "Readiness monitors not reported")]
    incomplete = [t.label for t in tests if not t.complete]
    if not incomplete:
        return [Finding("readiness", "Readiness", "ok", "All readiness monitors complete",
                        f"{len(tests)} monitors complete.", f"{len(tests)}/{len(tests)}")]
    since_km = s.counters.get("DISTANCE_SINCE_DTC_CLEAR")
    warmups = s.counters.get("WARMUPS_SINCE_DTC_CLEAR")
    value = f"{len(tests) - len(incomplete)}/{len(tests)}"
    recently = (since_km is not None and since_km < 100) or (warmups is not None and warmups < 10)
    if recently:
        extra = []
        if since_km is not None:
            extra.append(f"{since_km:.0f} km")
        if warmups is not None:
            extra.append(f"{warmups:.0f} warm-ups")
        return [Finding("readiness", "Readiness", "warn", "Trouble codes were cleared recently",
                        f"Incomplete monitors: {', '.join(incomplete)}. Only {' and '.join(extra)} since the "
                        "codes were cleared (or the battery was disconnected). Faults may be hidden — "
                        "when buying a used car, ask why.", value, 10)]
    return [Finding("readiness", "Readiness", "info", "Some readiness monitors are incomplete",
                    f"Incomplete: {', '.join(incomplete)}. Some cars never complete certain monitors "
                    "in normal driving.", value, 3)]


def _running(s: Snapshot) -> bool:
    rpm = s.values("RPM")
    return bool(rpm) and statistics.median(rpm) > 300


def _warm(s: Snapshot) -> Optional[bool]:
    coolant = s.values("COOLANT_TEMP")
    if not coolant:
        return None
    return max(coolant) >= 70


def _rule_charging(s: Snapshot) -> list[Finding]:
    volts = s.values("CONTROL_MODULE_VOLTAGE")
    source = "control module"
    if not volts and s.adapter_voltage:
        volts, source = [s.adapter_voltage], "adapter (OBD socket)"
    if not volts:
        return [Finding("charging", "Electrical", "skipped", "Voltage not available")]
    v = statistics.median(volts)
    value = _fmt(v, "V", 2)
    if _running(s):
        if v < 13.0:
            return [Finding("charging", "Electrical", "critical", "Battery is not being charged",
                            f"{source} voltage {value} with the engine running. Check the alternator, "
                            "its belt and wiring.", value, 20)]
        if v < 13.4:
            return [Finding("charging", "Electrical", "warn", "Low charging voltage",
                            f"{value} with the engine running (expected 13.4–14.8 V). The alternator may be weak "
                            "or many consumers are on.", value, 8)]
        if v > 15.2:
            return [Finding("charging", "Electrical", "critical", "Overcharging",
                            f"{value} with the engine running. Faulty voltage regulator can damage the battery "
                            "and electronics.", value, 20)]
        if v > 14.8:
            return [Finding("charging", "Electrical", "warn", "Charging voltage is high",
                            f"{value} (expected 13.4–14.8 V). Normal for some smart alternators in a cold "
                            "battery phase; otherwise check the regulator.", value, 5)]
        return [Finding("charging", "Electrical", "ok", "Charging system OK",
                        f"{value} with the engine running.", value)]
    # engine off: resting battery voltage
    if v < 11.8:
        return [Finding("battery", "Electrical", "critical", "Battery is discharged",
                        f"{value} with the engine off.", value, 15)]
    if v < 12.3:
        return [Finding("battery", "Electrical", "warn", "Battery is low", f"{value} with the engine off "
                        "(a charged battery rests at about 12.6 V).", value, 5)]
    return [Finding("battery", "Electrical", "ok", "Battery voltage OK", f"{value} with the engine off.", value)]


def _rule_cooling(s: Snapshot) -> list[Finding]:
    coolant = s.values("COOLANT_TEMP")
    if not coolant:
        return [Finding("cooling", "Cooling", "skipped", "Coolant temperature not available")]
    hi = max(coolant)
    value = _fmt(hi, "°C", 0)
    if hi > 108:
        return [Finding("cooling", "Cooling", "critical", "Engine is overheating",
                        f"Coolant reached {value}. Check coolant level, fan, thermostat and water pump.", value, 25)]
    if hi > 104:
        return [Finding("cooling", "Cooling", "warn", "Coolant temperature is high",
                        f"{value} (normal is about 85–100 °C).", value, 8)]
    run_time = s.counters.get("RUN_TIME")
    if hi < 75 and run_time is not None and run_time > 900 and _running(s):
        return [Finding("cooling", "Cooling", "warn", "Engine does not reach operating temperature",
                        f"Only {value} after {run_time / 60:.0f} min of running. The thermostat may be stuck open.",
                        value, 8)]
    if hi < 70:
        return [Finding("cooling", "Cooling", "skipped", "Engine is not warm yet",
                        f"Coolant {value}. Warm the engine up (>70 °C) for the fuel trim and O2 sensor checks.",
                        value)]
    return [Finding("cooling", "Cooling", "ok", "Coolant temperature OK", f"{value}.", value)]


def _rule_fuel_trims(s: Snapshot) -> list[Finding]:
    out = []
    for bank in ("1", "2"):
        stft, ltft = s.values(f"SHORT_FUEL_TRIM_{bank}"), s.values(f"LONG_FUEL_TRIM_{bank}")
        if not stft and not ltft:
            continue
        key = f"fuel_trim_b{bank}"
        if not _running(s) or not _warm(s):
            out.append(Finding(key, "Fuel mixture", "skipped", f"Fuel trims bank {bank} not evaluated",
                               "Needs a running, warm engine."))
            continue
        st = round(statistics.mean(stft), 1) + 0.0 if stft else 0.0  # + 0.0 turns -0.0 into 0.0
        lt = round(statistics.mean(ltft), 1) + 0.0 if ltft else 0.0
        total = round(st + lt, 1) + 0.0
        value = f"{total:+.1f} % (ST {st:+.1f}, LT {lt:+.1f})"
        side = "lean (ECU adds fuel)" if total > 0 else "rich (ECU removes fuel)"
        hint = ("Typical causes: vacuum/intake leak, weak fuel pump or clogged filter, dirty MAF sensor."
                if total > 0 else
                "Typical causes: leaking injectors, high fuel pressure, faulty purge valve, oil in the intake.")
        if abs(total) > 20:
            out.append(Finding(key, "Fuel mixture", "critical", f"Mixture far too {side.split()[0]} (bank {bank})",
                               f"Total fuel trim {total:+.1f} %: the engine runs {side}. {hint}", value, 20))
        elif abs(total) > 10:
            out.append(Finding(key, "Fuel mixture", "warn", f"Mixture is {side.split()[0]} (bank {bank})",
                               f"Total fuel trim {total:+.1f} % (normal within ±10 %). {hint}", value, 8))
        else:
            out.append(Finding(key, "Fuel mixture", "ok", f"Fuel trims bank {bank} OK",
                               f"Total fuel trim {total:+.1f} % (within ±10 %).", value))
    return out


def _idle_values(s: Snapshot, key: str) -> list[float]:
    """Values of `key` taken while the car was standing still."""
    speeds = dict(s.samples.get("SPEED", []))
    out = []
    for t, v in s.samples.get(key, []):
        # SPEED is read in the same batch, a few ms earlier: match the nearest sample
        nearest = min(speeds, key=lambda ts: abs(ts - t)) if speeds else None
        if nearest is None or speeds[nearest] < 1:
            out.append(v)
    return out


def _rule_idle(s: Snapshot) -> list[Finding]:
    if not _running(s):
        return [Finding("idle", "Idle", "skipped", "Engine is not running", "Start the engine for the idle checks.")]
    rpm = _idle_values(s, "RPM")
    if len(rpm) < 5:
        return [Finding("idle", "Idle", "skipped", "Idle not evaluated", "The car was moving during the check.")]
    out = []
    mean = statistics.mean(rpm)
    sd = statistics.pstdev(rpm)
    cv = sd / mean * 100 if mean else 0
    value = f"{mean:.0f} rpm ± {sd:.0f}"
    if cv > 10:
        out.append(Finding("idle_stability", "Idle", "critical", "Very rough idle",
                           f"Engine speed varies by ±{cv:.0f} %. Possible misfires, vacuum leak or dirty throttle body.",
                           value, 15))
    elif cv > 5:
        out.append(Finding("idle_stability", "Idle", "warn", "Unstable idle",
                           f"Engine speed varies by ±{cv:.0f} % (normal below 5 %).", value, 6))
    else:
        out.append(Finding("idle_stability", "Idle", "ok", "Stable idle", f"Variation ±{cv:.1f} %.", value))
    if _warm(s) and mean > 1100:
        out.append(Finding("idle_speed", "Idle", "info", "High idle speed",
                           f"{mean:.0f} rpm with a warm engine. Check for air leaks or idle control issues "
                           "(A/C or electrical load can raise idle).", f"{mean:.0f} rpm", 2))

    map_kpa = _idle_values(s, "INTAKE_PRESSURE")
    if map_kpa and _warm(s):
        m = statistics.median(map_kpa)
        mv = _fmt(m, "kPa", 0)
        if m > 50:
            out.append(Finding("idle_map", "Idle", "warn", "Low intake vacuum at idle",
                               f"Manifold pressure {mv} at idle (typical 25–45 kPa). Possible vacuum leak, "
                               "valve timing or compression problem.", mv, 8))
        else:
            out.append(Finding("idle_map", "Idle", "ok", "Intake vacuum at idle OK", f"Manifold pressure {mv}.", mv))
    return out


def _rule_o2(s: Snapshot) -> list[Finding]:
    up = s.samples.get("O2_B1S1", [])
    if not up:
        return []
    if not _running(s) or not _warm(s):
        return [Finding("o2_upstream", "O2 sensors", "skipped", "O2 sensors not evaluated",
                        "Needs a running, warm engine.")]
    out = []
    up_v = [v for _, v in up]
    up_span = (up[-1][0] - up[0][0]) or 1.0
    up_x = _crossings(up_v, 0.3, 0.6)
    up_hz = up_x / 2 / up_span  # two crossings per rich/lean cycle
    amplitude = max(up_v) - min(up_v)
    value = f"{up_hz:.2f} Hz, {min(up_v):.2f}–{max(up_v):.2f} V"
    if amplitude < 0.3:
        mean = statistics.mean(up_v)
        state = "lean" if mean < 0.45 else "rich"
        out.append(Finding("o2_upstream", "O2 sensors", "warn", "Upstream O2 sensor is not switching",
                           f"Stuck {state} around {mean:.2f} V. The sensor may be worn out, or the mixture is "
                           "far off (see fuel trims).", value, 10))
    elif up_hz < 0.15:
        out.append(Finding("o2_upstream", "O2 sensors", "warn", "Upstream O2 sensor is slow",
                           f"Only {up_hz:.2f} rich/lean cycles per second (a healthy sensor at idle switches "
                           "roughly every 1–3 s or faster). An aging sensor slows down fuel control.", value, 6))
    else:
        out.append(Finding("o2_upstream", "O2 sensors", "ok", "Upstream O2 sensor is switching",
                           f"{up_hz:.2f} cycles/s between {min(up_v):.2f} and {max(up_v):.2f} V.", value))

    down = [v for _, v in s.samples.get("O2_B1S2", [])]
    if down and amplitude >= 0.3:
        down_amp = max(down) - min(down)
        down_x = _crossings(down, 0.3, 0.6)
        ratio = down_x / up_x if up_x else 0.0
        dvalue = f"{min(down):.2f}–{max(down):.2f} V, switch ratio {ratio:.2f}"
        if down_amp > 0.5 and ratio > 0.6:
            out.append(Finding("catalyst", "Catalyst", "warn", "Catalyst efficiency may be low",
                               "The downstream O2 sensor follows the upstream one, so the catalyst stores little "
                               "oxygen. Confirm with a warm engine at 2500 rpm; often precedes code P0420.",
                               dvalue, 10))
        else:
            out.append(Finding("catalyst", "Catalyst", "ok", "Catalyst looks OK",
                               "The downstream O2 sensor is steady while the upstream one switches.", dvalue))
    return out


def _limits(t) -> str:
    if t.min is not None and t.max is not None:
        return f"allowed {t.min:g}–{t.max:g}"
    if t.max is not None:
        return f"limit {t.max:g}"
    if t.min is not None:
        return f"minimum {t.min:g}"
    return "no limit"


def _rule_monitors(s: Snapshot) -> list[Finding]:
    """On-board monitor test results (mode 06): failed tests, tests close to the limit, misfire counters."""
    if s.monitors is None:
        return []
    if not s.monitors.supported or not s.monitors.tests:
        return [Finding("monitor_tests", "Monitor tests", "skipped", "Monitor test results not available",
                        "The car does not report on-board test results (mode 06), or no test has run "
                        "since the codes were cleared.")]
    out = []
    unit = lambda t: f" {t.unit}" if t.unit else ""  # noqa: E731
    for t in s.monitors.tests:
        value = f"{t.value:g}{unit(t)}"
        if t.status == "fail":
            out.append(Finding(f"monitor_{t.id}", "Monitor tests", "critical", f"{t.monitor}: test failed",
                               f"{t.name}: {value} ({_limits(t)}{unit(t)}). The ECU's own test is outside its "
                               "limit; a trouble code follows if it fails again.", value, 10))
        elif t.status == "near":
            out.append(Finding(f"monitor_{t.id}", "Monitor tests", "warn", f"{t.monitor}: close to the limit",
                               f"{t.name}: {value} ({_limits(t)}{unit(t)}), only {t.margin:.0f} % headroom left. "
                               "No trouble code yet, but the component is wearing out.", value, 5))
        elif t.mid in MISFIRE_MIDS and t.value > 0 and t.mid != 0xA1:
            out.append(Finding(f"monitor_{t.id}", "Monitor tests", "warn", f"{t.monitor}: misfires counted",
                               f"{t.name}: {t.value:g}. Check the spark plug, coil and injector of this cylinder.",
                               f"{t.value:g}", 5))
    if not out:
        n = len(s.monitors.tests)
        out.append(Finding("monitor_tests", "Monitor tests", "ok", "On-board monitor tests passed",
                           f"{n} test results are within the ECU's limits with headroom.", f"{n}/{n}"))
    return out


RULES = [_rule_dtcs, _rule_readiness, _rule_monitors, _rule_charging, _rule_cooling, _rule_fuel_trims, _rule_idle,
         _rule_o2]


def evaluate(snapshot: Snapshot) -> HealthReport:
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(snapshot))
    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])

    score = 100 - sum(f.penalty for f in findings)
    if any(f.severity == "critical" for f in findings):
        score = min(score, 59)
    elif any(f.severity == "warn" for f in findings):
        score = min(score, 84)
    score = max(0, min(100, score))
    grade = "Good" if score >= 85 else "Fair" if score >= 60 else "Poor"
    samples = sum(len(v) for v in snapshot.samples.values())
    return HealthReport(score=score, grade=grade, findings=findings,
                        created=datetime.now().isoformat(timespec="seconds"),
                        duration=snapshot.duration, samples=samples, vehicle=snapshot.vehicle,
                        freeze_frame=snapshot.freeze.to_dict() if snapshot.freeze and snapshot.freeze.present else None,
                        monitor_tests=[t.to_dict() for t in snapshot.monitors.tests] if snapshot.monitors else [])


def run_check(session, duration: float = 30.0, progress: Optional[ProgressFn] = None) -> HealthReport:
    return evaluate(collect(session, duration, progress))
