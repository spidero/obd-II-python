"""The Polish dictionary must cover every text the web UI can show."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from i18n_keys import STATIC, html_keys, js_keys
from obd_tool import about
from obd_tool.core import ConnectionSettings, ObdSession, diag, dtc
from obd_tool.core.health import DISCLAIMER, HealthReport, evaluate, run_check
from obd_tool.core.models import MonitorReport
from obd_tool.core.pids import CATALOG, READINESS_LABELS
from obd_tool.core.report_html import render_html
from obd_tool.core.trip import GROUP, TRIP_PARAMS
from obd_tool.i18n import finding_text, tr
from obd_tool.web.server import create_app
from test_health import snapshot

PL = json.loads((STATIC / "pl.json").read_text(encoding="utf-8"))
STRINGS = PL["strings"]


def translated(text: str) -> bool:
    return text in STRINGS or any(re.match(pattern, text) for pattern, _ in PL["patterns"])


def missing(texts) -> list[str]:
    return sorted(t for t in set(texts) if t and not translated(t))


def test_static_page_and_script_texts_are_translated():
    assert missing(html_keys()) == []
    assert missing(js_keys()) == []


def test_placeholders_survive_translation():
    for english, polish in STRINGS.items():
        assert sorted(re.findall(r"\{\w+\}", english)) == sorted(re.findall(r"\{\w+\}", polish)), english
        assert english.count("<strong>") == polish.count("<strong>"), english
    for pattern, replacement in PL["patterns"]:
        groups = re.compile(pattern).groups
        assert {int(n) for n in re.findall(r"\$(\d)", replacement)} == set(range(1, groups + 1)), pattern


def test_server_labels_are_translated():
    texts = [v[0] for v in CATALOG.values()] + [v[2] for v in CATALOG.values()] + ["Other", GROUP]
    texts += [v[0] for v in TRIP_PARAMS.values()] + list(READINESS_LABELS.values())
    texts += list(diag.MONITOR_NAMES.values()) + [diag.monitor_name(0xEE)]
    texts += [diag.test_name(tid) for tid in (*range(1, 13), 0x80, 0xFF)]
    texts += [dtc.system_of(code) for code in ("P0133", "P0301", "P0420", "P0700", "P1234", "P2096", "P3401",
                                               "C0035", "C1234", "B0001", "U0100", "U1000")]
    texts += [a[k] for a in about.ADAPTERS for k in ("name", "notes")] + [about.VEHICLES_NOTE, DISCLAIMER]
    texts += ["Good", "Fair", "Poor", "spark (petrol)", "compression (diesel)", "Simulator", "My car", "Car 000002"]
    texts += [f"{{n}} {word}" for word in ("critical", "warning", "info", "ok", "not evaluated", "pass",
                                           "near limit", "fail")]
    texts += ["Reading status and trouble codes", "Reading freeze frame and monitor tests", "Sampling live data",
              "Sampling O2 sensors", "Evaluating"]
    assert missing(texts) == []


def _all_findings():
    """Health findings from every branch the unit tests and the simulator faults reach."""
    cases = [
        snapshot(),
        snapshot(stored=["P0301"], pending=["P0171"], mil=True, counters={"DISTANCE_W_MIL": 500, "RUN_TIME": 1200}),
        snapshot(incomplete=["CATALYST"]),
        snapshot(incomplete=["CATALYST"], counters={"DISTANCE_SINCE_DTC_CLEAR": 5, "WARMUPS_SINCE_DTC_CLEAR": 1}),
        snapshot(incomplete=["CATALYST"], counters={"DISTANCE_SINCE_DTC_CLEAR": 5}),
        snapshot(incomplete=["CATALYST"], counters={"WARMUPS_SINCE_DTC_CLEAR": 1}),
        snapshot({"COOLANT_TEMP": [40] * 8}, counters={"RUN_TIME": 60}),
        snapshot({"COOLANT_TEMP": [65] * 8}, counters={"RUN_TIME": 1800}),
        snapshot({"COOLANT_TEMP": [106] * 8}), snapshot({"COOLANT_TEMP": [112] * 8}),
        snapshot({"RPM": [0] * 8, "CONTROL_MODULE_VOLTAGE": [12.0] * 8}),
        snapshot({"RPM": [0] * 8, "CONTROL_MODULE_VOLTAGE": [11.0] * 8}),
        snapshot({"RPM": [0] * 8, "CONTROL_MODULE_VOLTAGE": [12.6] * 8}),
        snapshot({"SPEED": [50] * 8}),
        snapshot({"RPM": [700, 900, 600, 1000, 650, 950, 700, 900], "INTAKE_PRESSURE": [60] * 8}),
        snapshot({"RPM": [700, 760, 660, 750, 680, 760, 690, 750]}),
        snapshot({"RPM": [1300] * 8}),
        snapshot({"O2_B1S1": [0.1, 0.8] * 4, "O2_B1S2": [0.1, 0.8] * 4}),
        snapshot({"O2_B1S1": [0.1, 0.8] * 4, "O2_B1S2": [0.7] * 8}),
        snapshot({"O2_B1S1": [0.1] * 8}), snapshot({"O2_B1S1": [0.8] * 8}),
        snapshot({"O2_B1S1": [0.1] * 4 + [0.8] * 4}),
    ]
    for volts in (13.2, 12.5, 15.0, 15.5):
        cases.append(snapshot({"CONTROL_MODULE_VOLTAGE": [volts] * 8}))
    for st, lt in ((8, 6), (12, 10), (-8, -6), (-12, -10)):
        cases.append(snapshot({"SHORT_FUEL_TRIM_1": [st] * 8, "LONG_FUEL_TRIM_1": [lt] * 8}))
    cases.append(snapshot())
    cases[-1].monitors = MonitorReport(supported=False)
    findings = [f for case in cases for f in evaluate(case).findings]
    for faults, codes in ((("rough_idle",), True), ((), False)):
        s = ObdSession(ConnectionSettings(simulate=True, sim_faults=faults, sim_codes=codes))
        s.connect()
        try:
            findings += run_check(s, 3).findings
            if codes:  # a permanent code left behind after clearing
                s.clear_dtcs()
                findings += run_check(s, 3).findings
        finally:
            s.close()
    return findings


def test_health_findings_are_translated():
    findings = _all_findings()
    assert len({f.title_t for f in findings}) > 45   # the cases above reach nearly every rule branch
    texts = [x for f in findings for x in (f.title_t, f.detail_t, f.area)]
    # sub-phrases like "control module" or a monitor name; DTC descriptions stay in English
    texts += [v for f in findings for k, v in f.params.items()
              if isinstance(v, str) and k not in ("description", "value", "conditions") and re.search("[a-z]{4}", v)]
    texts += [x for f in findings for v in f.params.values() if isinstance(v, list) for x in v]
    assert missing(texts) == []
    for f in findings:
        polish = finding_text(f.to_dict(), "pl")
        assert "{" not in polish["title"] + polish["detail"], f.key
        english = finding_text(f.to_dict(), "en")
        assert (english["title"], english["detail"]) == (f.title, f.detail)   # templates rebuild the English text


def test_translate():
    assert tr("Catalyst bank 1", "pl") == "Katalizator, bank 1"
    assert tr("O2 sensor heater B1S1", "pl") == "Grzałka sondy O2 B1S1"
    assert tr("Powertrain: Fuel and air metering", "pl") == "Układ napędowy: Dozowanie paliwa i powietrza"
    assert tr("Stored code {code}", "pl", code="P0420") == "Zapisany kod P0420"
    assert tr("Catalyst bank 1", "en") == "Catalyst bank 1" == tr("Catalyst bank 1", "de")   # unknown: English
    assert tr("something new", "pl") == "something new"


def test_printable_report_in_polish():
    report = evaluate(snapshot(stored=["P0301"], mil=True))
    polish, english = render_html(report, "pl"), render_html(report)
    assert 'lang="pl"' in polish and "Raport stanu auta" in polish and "Zapisany kod P0301" in polish
    assert "Kontrolka silnika (MIL) świeci" in polish and "Vehicle health report" not in polish
    assert "Vehicle health report" in english and "Stored code P0301" in english
    # a report saved before templates existed still renders
    old = report.to_dict()
    for f in old["findings"]:
        for key in ("title_t", "detail_t", "params"):
            f.pop(key)
    assert "Stored code P0301" in render_html(HealthReport.from_dict(old), "pl")


def test_dictionary_is_served():
    with TestClient(create_app(ConnectionSettings(), connect=False)) as client:
        assert client.get("/pl.json").json()["strings"]["Dashboard"] == "Pulpit"
        assert 'id="btn-lang"' in client.get("/").text
