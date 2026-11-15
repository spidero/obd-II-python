"""Battery and charging system test: battery at rest, voltage dip while cranking, charging at idle and
under load. Voltage is read by the adapter at the OBD socket (AT RV), which is wired straight to the battery.

The test is guided: `run_battery_test` asks the user to do something (`ask`) before each step, so the same
code drives the CLI (press Enter) and the web UI (a button).
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .health import SEVERITY_ORDER, F, Finding, HealthReport, score_findings

AREA_BATTERY, AREA_CHARGING = "Battery", "Charging"
STEPS = ("rest", "crank", "idle", "load")

PROMPTS = {
    "rest": "Switch the ignition ON but do not start the engine; switch off lights and other consumers.",
    "crank": "Start the engine now – the voltage is recorded while it cranks.",
    "idle": "Let the engine idle.",
    "load": "Hold about 2000 rpm and switch on the headlights, the rear window heater and the blower.",
}

# resting voltage -> approximate state of charge of a lead-acid battery (ignition on lowers it by ~0.1 V)
SOC_TABLE = [(12.65, 100), (12.45, 75), (12.24, 50), (12.06, 25), (11.89, 0)]


def state_of_charge(volts: float) -> int:
    for (v_hi, soc_hi), (v_lo, soc_lo) in zip(SOC_TABLE, SOC_TABLE[1:]):
        if volts >= v_lo:
            return round(soc_lo + (min(volts, v_hi) - v_lo) / (v_hi - v_lo) * (soc_hi - soc_lo))
    return 0


@dataclass
class BatteryData:
    rest: list[float] = field(default_factory=list)       # volts, engine off
    crank: list[float] = field(default_factory=list)      # volts while starting
    started: bool = False                                  # the engine started during the crank step
    crank_lost: bool = False                               # the adapter dropped out while cranking
    engine_was_running: bool = False                       # the test began with the engine running
    idle: list[float] = field(default_factory=list)
    load: list[float] = field(default_factory=list)
    load_rpm: list[float] = field(default_factory=list)
    duration: float = 0.0
    vehicle: dict = field(default_factory=dict)


def _v(x: float) -> str:
    return f"{x:.2f} V"


def _rule_rest(d: BatteryData) -> list[Finding]:
    if d.engine_was_running or not d.rest:
        return [F("battery_rest", AREA_BATTERY, "skipped", "Battery at rest not measured",
                  "Start the test with the engine off (ignition on) to check the battery itself.")]
    v = statistics.median(d.rest)
    p = {"value": _v(v), "soc": str(state_of_charge(v))}
    if v < 12.1:
        return [F("battery_rest", AREA_BATTERY, "critical", "Battery is discharged",
                  "{value} with the engine off – about {soc} % charged. Charge it and test again; a battery that "
                  "keeps going flat is worn out or something drains it while parked.", p["value"], 20, **p)]
    if v < 12.4:
        return [F("battery_rest", AREA_BATTERY, "warn", "Battery is not fully charged",
                  "{value} with the engine off – about {soc} % charged. Short trips do not recharge it; "
                  "charge it or drive longer.", p["value"], 8, **p)]
    return [F("battery_rest", AREA_BATTERY, "ok", "Battery charged",
              "{value} with the engine off – about {soc} % charged.", p["value"], **p)]


def _rule_crank(d: BatteryData) -> list[Finding]:
    if d.engine_was_running:
        return [F("battery_crank", AREA_BATTERY, "skipped", "Start not measured",
                  "Start the test with the engine off to see how the battery copes with starting.")]
    if d.crank_lost and d.crank:
        v = min(d.crank)
        return [F("battery_crank", AREA_BATTERY, "warn", "Adapter dropped out while starting",
                  "The voltage fell so low during the start that the adapter lost power (last reading {value}). "
                  "This points to a weak battery – have it load-tested.", _v(v), 10, value=_v(v))]
    if not d.crank or not d.started:
        return [F("battery_crank", AREA_BATTERY, "skipped", "Start not measured",
                  "The engine was not started during the start step.")]
    v = min(d.crank)
    p = {"value": _v(v)}
    if v < 9.6:
        return [F("battery_crank", AREA_BATTERY, "critical", "Battery too weak for reliable starts",
                  "The voltage dropped to {value} while starting (should stay above 9.6 V). The battery may "
                  "fail on a cold morning – replace it or have it load-tested.", p["value"], 20, **p)]
    if v < 10.0:
        return [F("battery_crank", AREA_BATTERY, "warn", "Battery is getting weak",
                  "The voltage dropped to {value} while starting (above 10 V is healthy). Watch it before "
                  "winter.", p["value"], 10, **p)]
    return [F("battery_crank", AREA_BATTERY, "ok", "Battery handles the start well",
              "Lowest voltage while starting: {value}.", p["value"], **p)]


def _rule_idle(d: BatteryData) -> list[Finding]:
    if not d.idle:
        return [F("charging_idle", AREA_CHARGING, "skipped", "Charging not measured",
                  "The engine was not running during the idle step.")]
    v = statistics.median(d.idle)
    swing = max(d.idle) - min(d.idle)
    p = {"value": _v(v)}
    out = []
    if v < 12.9:
        out.append(F("charging_idle", AREA_CHARGING, "critical", "Alternator is not charging",
                     "{value} at idle – the battery is being drained. Check the alternator, its belt, the charge "
                     "warning light and the wiring.", p["value"], 25, **p))
    elif v < 13.4:
        out.append(F("charging_idle", AREA_CHARGING, "warn", "Low charging voltage at idle",
                     "{value} at idle (expected 13.4–14.8 V). Normal for cars with smart charging when the battery "
                     "is full; otherwise the alternator or regulator is weak.", p["value"], 8, **p))
    elif v > 15.2:
        out.append(F("charging_idle", AREA_CHARGING, "critical", "Overcharging",
                     "{value} at idle – a faulty voltage regulator boils the battery and can damage electronics.",
                     p["value"], 25, **p))
    elif v > 14.8:
        out.append(F("charging_idle", AREA_CHARGING, "warn", "Charging voltage is high",
                     "{value} at idle (expected 13.4–14.8 V). Can be normal right after a start in the cold; "
                     "otherwise check the regulator.", p["value"], 5, **p))
    else:
        out.append(F("charging_idle", AREA_CHARGING, "ok", "Alternator charges at idle", "{value} at idle.",
                     p["value"], **p))
    if len(d.idle) >= 5 and swing > 0.6:
        out.append(F("charging_ripple", AREA_CHARGING, "warn", "Charging voltage fluctuates",
                     "The voltage swings by {swing} at a steady idle. Typical for a failing regulator, a worn "
                     "brush set or a damaged diode in the alternator.", _v(swing), 6, swing=_v(swing)))
    return out


def _rule_load(d: BatteryData) -> list[Finding]:
    if not d.load:
        return [F("charging_load", AREA_CHARGING, "skipped", "Charging under load not measured",
                  "Skipped – repeat with about 2000 rpm and consumers on to check the alternator's output.")]
    v = statistics.median(d.load)
    rpm = statistics.median(d.load_rpm) if d.load_rpm else 0
    p = {"value": _v(v), "rpm": f"{rpm:.0f}"}
    out = []
    if rpm and rpm < 1500:
        out.append(F("charging_load_rpm", AREA_CHARGING, "info", "Engine speed too low for the load step",
                     "Only {rpm} rpm – hold about 2000 rpm so the alternator can deliver its full output.",
                     f"{rpm:.0f} rpm", **p))
    if v < 13.0:
        out.append(F("charging_load", AREA_CHARGING, "critical", "Alternator cannot keep up under load",
                     "{value} at {rpm} rpm with consumers on. The battery is discharging while driving – the "
                     "alternator is weak or its belt slips.", p["value"], 20, **p))
    elif v < 13.4 or (d.idle and statistics.median(d.idle) - v > 0.6):
        out.append(F("charging_load", AREA_CHARGING, "warn", "Charging voltage drops under load",
                     "{value} at {rpm} rpm with consumers on. The alternator is at its limit; check the belt "
                     "and the alternator output.", p["value"], 8, **p))
    else:
        out.append(F("charging_load", AREA_CHARGING, "ok", "Alternator keeps up under load",
                     "{value} at {rpm} rpm with consumers on.", p["value"], **p))
    return out


RULES = [_rule_rest, _rule_crank, _rule_idle, _rule_load]


def evaluate_battery(d: BatteryData) -> HealthReport:
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(d))
    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    score, grade = score_findings(findings)
    samples = len(d.rest) + len(d.crank) + len(d.idle) + len(d.load)
    return HealthReport(score=score, grade=grade, findings=findings,
                        created=datetime.now().isoformat(timespec="seconds"), duration=d.duration, samples=samples,
                        vehicle=d.vehicle, kind="battery")


# --------------------------------------------------------------------------- running the test
Ask = Callable[[str, str], bool]                   # (step, prompt) -> False to skip the step
Progress = Callable[[float, str, Optional[float]], None]  # (fraction, message, latest volts)


def run_battery_test(session, ask: Ask, progress: Optional[Progress] = None, *,
                     rest_s: float = 3.0, crank_timeout_s: float = 20.0, idle_s: float = 15.0,
                     load_s: float = 10.0) -> HealthReport:
    """Guide the user through the steps and evaluate. `session` needs adapter_voltage() and read()."""
    progress = progress or (lambda f, m, v=None: None)
    d = BatteryData()
    t0 = time.monotonic()
    try:
        d.vehicle = {"adapter": session.adapter_info().to_dict()}
    except Exception:
        pass

    def rpm() -> float:
        r = session.read(["RPM"])[0]
        return float(r.value) if isinstance(r.value, (int, float)) else 0.0

    def sample(seconds: float, into: list, frac0: float, frac1: float, message: str, rpm_into: list | None = None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            v = session.adapter_voltage()
            if v is not None:
                into.append(v)
            if rpm_into is not None:
                rpm_into.append(rpm())
            done = 1 - (end - time.monotonic()) / seconds
            progress(frac0 + (frac1 - frac0) * min(1.0, done), message, v)

    d.engine_was_running = rpm() > 300
    if not d.engine_was_running:
        if ask("rest", PROMPTS["rest"]):
            sample(rest_s, d.rest, 0.0, 0.15, "Measuring the battery at rest")
        if ask("crank", PROMPTS["crank"]):
            end = time.monotonic() + crank_timeout_s
            running_reads = 0
            n = 0
            while time.monotonic() < end:
                try:
                    v = session.adapter_voltage()
                    if v is not None:
                        d.crank.append(v)
                    n += 1
                    if n % 4 == 0:                       # mostly voltage, sometimes rpm: catch the dip
                        running_reads = running_reads + 1 if rpm() > 400 else 0
                        if running_reads >= 2:
                            d.started = True
                            break
                    progress(0.15 + 0.3 * (1 - (end - time.monotonic()) / crank_timeout_s),
                             "Recording the start", v)
                except Exception:
                    d.crank_lost = True
                    break
    if d.crank_lost:
        d.duration = time.monotonic() - t0
        return evaluate_battery(d)
    if d.started or d.engine_was_running or rpm() > 300:
        ask("idle", PROMPTS["idle"])
        sample(idle_s, d.idle, 0.45, 0.75, "Measuring the charging voltage at idle")
        if ask("load", PROMPTS["load"]):
            sample(load_s, d.load, 0.75, 1.0, "Measuring the charging voltage under load", d.load_rpm)
    progress(1.0, "Evaluating", None)
    d.duration = time.monotonic() - t0
    return evaluate_battery(d)
