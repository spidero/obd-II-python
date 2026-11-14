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
    title_t: str = ""      # templates and parameters of title/detail, for showing them in another language
    detail_t: str = ""
    params: dict = field(default_factory=dict)

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
        fields = ("key", "area", "severity", "title", "detail", "value", "penalty", "title_t", "detail_t", "params")
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
# Every text is a template with {named} parameters, so the UI can show it in another
# language (see obd_tool/i18n.py). Parameters are ready-made strings or lists of strings.
def _render(template: str, params: dict) -> str:
    shown = {k: ", ".join(v) if isinstance(v, list) else v for k, v in params.items()}
    return template.format(**shown) if template else ""


def F(key: str, area: str, severity: str, title: str, detail: str = "", shown: str = "", penalty: int = 0, /,
      **params) -> Finding:
    """Build a finding; `shown` is the value column, `params` fill the {placeholders}."""
    return Finding(key, area, severity, _render(title, params), _render(detail, params), shown, penalty,
                   title_t=title, detail_t=detail, params=params)


NO_DESCRIPTION = "Manufacturer specific code"


def _rule_dtcs(s: Snapshot) -> list[Finding]:
    out = []
    stored, pending = s.dtcs.stored, s.dtcs.pending
    if s.status.mil:
        out.append(F("mil", "Trouble codes", "critical", "Check engine light (MIL) is on",
                     "The ECU has confirmed an emissions related fault.", "ON", 10))
    for d in stored:
        out.append(F(f"dtc_{d.code}", "Trouble codes", "critical", "Stored code {code}", "{description}",
                     d.system, 15, code=d.code, description=d.description or NO_DESCRIPTION))
    for d in pending:
        out.append(F(f"dtc_{d.code}", "Trouble codes", "warn", "Pending code {code}",
                     "{description} — detected once, not confirmed yet.", d.system, 5,
                     code=d.code, description=d.description or NO_DESCRIPTION))
    stored_codes = {d.code for d in stored}
    for d in s.dtcs.permanent:
        if d.code in stored_codes:
            continue  # already reported as a stored code
        out.append(F(f"permanent_{d.code}", "Trouble codes", "warn", "Permanent code {code}",
                     "{description} — the codes were cleared, but the ECU has not yet confirmed that this fault "
                     "is gone. It disappears by itself after the monitor passes.", d.system, 5,
                     code=d.code, description=d.description or NO_DESCRIPTION))
    if not stored and not pending and not s.status.mil:
        out.append(F("dtc", "Trouble codes", "ok", "No trouble codes", "No stored or pending codes, MIL off."))
    if s.freeze is not None and s.freeze.present:
        shown = {r.key: r for r in s.freeze.readings}
        parts = [f"{shown[k].value:g} {shown[k].unit}" for k in ("RPM", "SPEED", "COOLANT_TEMP", "ENGINE_LOAD")
                 if k in shown and isinstance(shown[k].value, (int, float))]
        detail = ("Engine conditions when the fault was detected: {conditions}. See the freeze frame table."
                  if parts else "Engine conditions when the fault was detected. See the freeze frame table.")
        out.append(F("freeze_frame", "Trouble codes", "info", "Freeze frame stored for {code}", detail,
                     s.freeze.dtc.code, code=s.freeze.dtc.code, conditions=", ".join(parts)))
    dist_mil = s.counters.get("DISTANCE_W_MIL")
    if dist_mil:
        out.append(F("distance_mil", "Trouble codes", "warn" if dist_mil > 100 else "info",
                     "Distance driven with MIL on", "The car was driven with the check engine light on.",
                     _fmt(dist_mil, "km", 0), 5 if dist_mil > 100 else 0))
    return out


def _rule_readiness(s: Snapshot) -> list[Finding]:
    tests = s.status.readiness
    if not tests:
        return [F("readiness", "Readiness", "skipped", "Readiness monitors not reported")]
    incomplete = [t.label for t in tests if not t.complete]
    if not incomplete:
        return [F("readiness", "Readiness", "ok", "All readiness monitors complete",
                  "{n} monitors complete.", f"{len(tests)}/{len(tests)}", n=str(len(tests)))]
    since_km = s.counters.get("DISTANCE_SINCE_DTC_CLEAR")
    warmups = s.counters.get("WARMUPS_SINCE_DTC_CLEAR")
    value = f"{len(tests) - len(incomplete)}/{len(tests)}"
    recently = (since_km is not None and since_km < 100) or (warmups is not None and warmups < 10)
    if recently:
        tail = (" since the codes were cleared (or the battery was disconnected). Faults may be hidden — "
                "when buying a used car, ask why.")
        if since_km is not None and warmups is not None:
            detail = "Incomplete monitors: {monitors}. Only {km} km and {warmups} warm-ups" + tail
        elif since_km is not None:
            detail = "Incomplete monitors: {monitors}. Only {km} km" + tail
        else:
            detail = "Incomplete monitors: {monitors}. Only {warmups} warm-ups" + tail
        return [F("readiness", "Readiness", "warn", "Trouble codes were cleared recently", detail, value, 10,
                  monitors=incomplete, km=f"{since_km or 0:.0f}", warmups=f"{warmups or 0:.0f}")]
    return [F("readiness", "Readiness", "info", "Some readiness monitors are incomplete",
              "Incomplete: {monitors}. Some cars never complete certain monitors in normal driving.",
              value, 3, monitors=incomplete)]


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
        return [F("charging", "Electrical", "skipped", "Voltage not available")]
    v = statistics.median(volts)
    value = _fmt(v, "V", 2)
    p = {"value": value, "source": source}
    if _running(s):
        if v < 13.0:
            return [F("charging", "Electrical", "critical", "Battery is not being charged",
                      "{source} voltage {value} with the engine running. Check the alternator, "
                      "its belt and wiring.", value, 20, **p)]
        if v < 13.4:
            return [F("charging", "Electrical", "warn", "Low charging voltage",
                      "{value} with the engine running (expected 13.4–14.8 V). The alternator may be weak "
                      "or many consumers are on.", value, 8, **p)]
        if v > 15.2:
            return [F("charging", "Electrical", "critical", "Overcharging",
                      "{value} with the engine running. Faulty voltage regulator can damage the battery "
                      "and electronics.", value, 20, **p)]
        if v > 14.8:
            return [F("charging", "Electrical", "warn", "Charging voltage is high",
                      "{value} (expected 13.4–14.8 V). Normal for some smart alternators in a cold "
                      "battery phase; otherwise check the regulator.", value, 5, **p)]
        return [F("charging", "Electrical", "ok", "Charging system OK",
                  "{value} with the engine running.", value, **p)]
    # engine off: resting battery voltage
    if v < 11.8:
        return [F("battery", "Electrical", "critical", "Battery is discharged",
                  "{value} with the engine off.", value, 15, **p)]
    if v < 12.3:
        return [F("battery", "Electrical", "warn", "Battery is low", "{value} with the engine off "
                  "(a charged battery rests at about 12.6 V).", value, 5, **p)]
    return [F("battery", "Electrical", "ok", "Battery voltage OK", "{value} with the engine off.", value, **p)]


def _rule_cooling(s: Snapshot) -> list[Finding]:
    coolant = s.values("COOLANT_TEMP")
    if not coolant:
        return [F("cooling", "Cooling", "skipped", "Coolant temperature not available")]
    hi = max(coolant)
    value = _fmt(hi, "°C", 0)
    if hi > 108:
        return [F("cooling", "Cooling", "critical", "Engine is overheating",
                  "Coolant reached {value}. Check coolant level, fan, thermostat and water pump.", value, 25,
                  value=value)]
    if hi > 104:
        return [F("cooling", "Cooling", "warn", "Coolant temperature is high",
                  "{value} (normal is about 85–100 °C).", value, 8, value=value)]
    run_time = s.counters.get("RUN_TIME")
    if hi < 75 and run_time is not None and run_time > 900 and _running(s):
        return [F("cooling", "Cooling", "warn", "Engine does not reach operating temperature",
                  "Only {value} after {minutes} min of running. The thermostat may be stuck open.",
                  value, 8, value=value, minutes=f"{run_time / 60:.0f}")]
    if hi < 70:
        return [F("cooling", "Cooling", "skipped", "Engine is not warm yet",
                  "Coolant {value}. Warm the engine up (>70 °C) for the fuel trim and O2 sensor checks.",
                  value, value=value)]
    return [F("cooling", "Cooling", "ok", "Coolant temperature OK", "{value}.", value, value=value)]


_LEAN_HINT = "Typical causes: vacuum/intake leak, weak fuel pump or clogged filter, dirty MAF sensor."
_RICH_HINT = "Typical causes: leaking injectors, high fuel pressure, faulty purge valve, oil in the intake."


def _rule_fuel_trims(s: Snapshot) -> list[Finding]:
    out = []
    for bank in ("1", "2"):
        stft, ltft = s.values(f"SHORT_FUEL_TRIM_{bank}"), s.values(f"LONG_FUEL_TRIM_{bank}")
        if not stft and not ltft:
            continue
        key = f"fuel_trim_b{bank}"
        if not _running(s) or not _warm(s):
            out.append(F(key, "Fuel mixture", "skipped", "Fuel trims bank {bank} not evaluated",
                         "Needs a running, warm engine.", bank=bank))
            continue
        st = round(statistics.mean(stft), 1) + 0.0 if stft else 0.0  # + 0.0 turns -0.0 into 0.0
        lt = round(statistics.mean(ltft), 1) + 0.0 if ltft else 0.0
        total = round(st + lt, 1) + 0.0
        value = f"{total:+.1f} % (ST {st:+.1f}, LT {lt:+.1f})"
        p = {"bank": bank, "total": f"{total:+.1f}"}
        lean = total > 0
        if abs(total) > 20:
            out.append(F(key, "Fuel mixture", "critical",
                         "Mixture far too lean (bank {bank})" if lean else "Mixture far too rich (bank {bank})",
                         ("Total fuel trim {total} %: the engine runs lean (ECU adds fuel). " + _LEAN_HINT) if lean
                         else ("Total fuel trim {total} %: the engine runs rich (ECU removes fuel). " + _RICH_HINT),
                         value, 20, **p))
        elif abs(total) > 10:
            out.append(F(key, "Fuel mixture", "warn",
                         "Mixture is lean (bank {bank})" if lean else "Mixture is rich (bank {bank})",
                         "Total fuel trim {total} % (normal within ±10 %). " + (_LEAN_HINT if lean else _RICH_HINT),
                         value, 8, **p))
        else:
            out.append(F(key, "Fuel mixture", "ok", "Fuel trims bank {bank} OK",
                         "Total fuel trim {total} % (within ±10 %).", value, **p))
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
        return [F("idle", "Idle", "skipped", "Engine is not running", "Start the engine for the idle checks.")]
    rpm = _idle_values(s, "RPM")
    if len(rpm) < 5:
        return [F("idle", "Idle", "skipped", "Idle not evaluated", "The car was moving during the check.")]
    out = []
    mean = statistics.mean(rpm)
    sd = statistics.pstdev(rpm)
    cv = sd / mean * 100 if mean else 0
    value = f"{mean:.0f} rpm ± {sd:.0f}"
    if cv > 10:
        out.append(F("idle_stability", "Idle", "critical", "Very rough idle",
                     "Engine speed varies by ±{cv} %. Possible misfires, vacuum leak or dirty throttle body.",
                     value, 15, cv=f"{cv:.0f}"))
    elif cv > 5:
        out.append(F("idle_stability", "Idle", "warn", "Unstable idle",
                     "Engine speed varies by ±{cv} % (normal below 5 %).", value, 6, cv=f"{cv:.0f}"))
    else:
        out.append(F("idle_stability", "Idle", "ok", "Stable idle", "Variation ±{cv} %.", value, cv=f"{cv:.1f}"))
    if _warm(s) and mean > 1100:
        out.append(F("idle_speed", "Idle", "info", "High idle speed",
                     "{rpm} rpm with a warm engine. Check for air leaks or idle control issues "
                     "(A/C or electrical load can raise idle).", f"{mean:.0f} rpm", 2, rpm=f"{mean:.0f}"))

    map_kpa = _idle_values(s, "INTAKE_PRESSURE")
    if map_kpa and _warm(s):
        m = statistics.median(map_kpa)
        mv = _fmt(m, "kPa", 0)
        if m > 50:
            out.append(F("idle_map", "Idle", "warn", "Low intake vacuum at idle",
                         "Manifold pressure {value} at idle (typical 25–45 kPa). Possible vacuum leak, "
                         "valve timing or compression problem.", mv, 8, value=mv))
        else:
            out.append(F("idle_map", "Idle", "ok", "Intake vacuum at idle OK", "Manifold pressure {value}.", mv,
                         value=mv))
    return out


def _rule_o2(s: Snapshot) -> list[Finding]:
    up = s.samples.get("O2_B1S1", [])
    if not up:
        return []
    if not _running(s) or not _warm(s):
        return [F("o2_upstream", "O2 sensors", "skipped", "O2 sensors not evaluated",
                  "Needs a running, warm engine.")]
    out = []
    up_v = [v for _, v in up]
    up_span = (up[-1][0] - up[0][0]) or 1.0
    up_x = _crossings(up_v, 0.3, 0.6)
    up_hz = up_x / 2 / up_span  # two crossings per rich/lean cycle
    amplitude = max(up_v) - min(up_v)
    value = f"{up_hz:.2f} Hz, {min(up_v):.2f}–{max(up_v):.2f} V"
    p = {"hz": f"{up_hz:.2f}", "lo": f"{min(up_v):.2f}", "hi": f"{max(up_v):.2f}"}
    if amplitude < 0.3:
        mean = statistics.mean(up_v)
        out.append(F("o2_upstream", "O2 sensors", "warn", "Upstream O2 sensor is not switching",
                     ("Stuck lean around {mean} V. " if mean < 0.45 else "Stuck rich around {mean} V. ")
                     + "The sensor may be worn out, or the mixture is far off (see fuel trims).",
                     value, 10, mean=f"{mean:.2f}"))
    elif up_hz < 0.15:
        out.append(F("o2_upstream", "O2 sensors", "warn", "Upstream O2 sensor is slow",
                     "Only {hz} rich/lean cycles per second (a healthy sensor at idle switches "
                     "roughly every 1–3 s or faster). An aging sensor slows down fuel control.", value, 6, **p))
    else:
        out.append(F("o2_upstream", "O2 sensors", "ok", "Upstream O2 sensor is switching",
                     "{hz} cycles/s between {lo} and {hi} V.", value, **p))

    down = [v for _, v in s.samples.get("O2_B1S2", [])]
    if down and amplitude >= 0.3:
        down_amp = max(down) - min(down)
        down_x = _crossings(down, 0.3, 0.6)
        ratio = down_x / up_x if up_x else 0.0
        dvalue = f"{min(down):.2f}–{max(down):.2f} V, switch ratio {ratio:.2f}"
        if down_amp > 0.5 and ratio > 0.6:
            out.append(F("catalyst", "Catalyst", "warn", "Catalyst efficiency may be low",
                         "The downstream O2 sensor follows the upstream one, so the catalyst stores little "
                         "oxygen. Confirm with a warm engine at 2500 rpm; often precedes code P0420.",
                         dvalue, 10))
        else:
            out.append(F("catalyst", "Catalyst", "ok", "Catalyst looks OK",
                         "The downstream O2 sensor is steady while the upstream one switches.", dvalue))
    return out


def _limits(t) -> str:
    unit = f" {t.unit}" if t.unit else ""
    if t.min is not None and t.max is not None:
        return f"allowed {t.min:g}–{t.max:g}{unit}"
    if t.max is not None:
        return f"limit {t.max:g}{unit}"
    if t.min is not None:
        return f"minimum {t.min:g}{unit}"
    return "no limit"


def _rule_monitors(s: Snapshot) -> list[Finding]:
    """On-board monitor test results (mode 06): failed tests, tests close to the limit, misfire counters."""
    if s.monitors is None:
        return []
    if not s.monitors.supported or not s.monitors.tests:
        return [F("monitor_tests", "Monitor tests", "skipped", "Monitor test results not available",
                  "The car does not report on-board test results (mode 06), or no test has run "
                  "since the codes were cleared.")]
    out = []
    for t in s.monitors.tests:
        value = f"{t.value:g}" + (f" {t.unit}" if t.unit else "")
        p = {"monitor": t.monitor, "test": t.name, "value": value, "limits": _limits(t)}
        if t.status == "fail":
            out.append(F(f"monitor_{t.id}", "Monitor tests", "critical", "{monitor}: test failed",
                         "{test}: {value} ({limits}). The ECU's own test is outside its "
                         "limit; a trouble code follows if it fails again.", value, 10, **p))
        elif t.status == "near":
            out.append(F(f"monitor_{t.id}", "Monitor tests", "warn", "{monitor}: close to the limit",
                         "{test}: {value} ({limits}), only {margin} % headroom left. "
                         "No trouble code yet, but the component is wearing out.", value, 5,
                         margin=f"{t.margin:.0f}", **p))
        elif t.mid in MISFIRE_MIDS and t.value > 0 and t.mid != 0xA1:
            out.append(F(f"monitor_{t.id}", "Monitor tests", "warn", "{monitor}: misfires counted",
                         "{test}: {value}. Check the spark plug, coil and injector of this cylinder.",
                         f"{t.value:g}", 5, **p))
    if not out:
        n = len(s.monitors.tests)
        out.append(F("monitor_tests", "Monitor tests", "ok", "On-board monitor tests passed",
                     "{n} test results are within the ECU's limits with headroom.", f"{n}/{n}", n=str(n)))
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
