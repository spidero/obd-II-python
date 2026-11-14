"""Standalone, printable HTML version of a health report."""

from __future__ import annotations

from html import escape

from .health import HealthReport

_COLORS = {"critical": "#d22f2f", "warn": "#b07a00", "info": "#2f6fd2", "ok": "#138a5b", "skipped": "#7a828c"}
_LABELS = {"critical": "Critical", "warn": "Warning", "info": "Info", "ok": "OK", "skipped": "Not evaluated"}
_STATUS = {"pass": ("Pass", "#138a5b"), "near": ("Near limit", "#b07a00"), "fail": ("Fail", "#d22f2f")}
_GRADE = {"Good": "#138a5b", "Fair": "#b07a00", "Poor": "#d22f2f"}


def _num(value) -> str:
    if value is None:
        return "—"
    return f"{value:g}" if isinstance(value, float) else str(value)


def render_html(report: HealthReport) -> str:
    v = report.vehicle or {}
    rows = []
    for f in report.findings:
        color = _COLORS[f.severity]
        rows.append(
            f'<tr><td><span class="sev" style="background:{color}">{_LABELS[f.severity]}</span></td>'
            f"<td>{escape(f.area)}</td><td><strong>{escape(f.title)}</strong>"
            f'<div class="detail">{escape(f.detail)}</div></td>'
            f'<td class="val">{escape(f.value)}</td></tr>'
        )
    meta = [
        ("Date", report.created.replace("T", " ")),
        ("VIN", v.get("vin") or "not reported"),
        ("Protocol", v.get("protocol") or "?"),
        ("Adapter", v.get("adapter") or "?"),
        ("Check duration", f"{report.duration:.0f} s, {report.samples} samples"),
    ]
    if v.get("simulated"):
        meta.append(("Source", "simulator"))
    meta_html = "".join(f"<dt>{escape(k)}</dt><dd>{escape(str(val))}</dd>" for k, val in meta)
    extra = ""
    ff = report.freeze_frame
    if ff and ff.get("dtc"):
        ff_rows = "".join(
            f"<tr><td>{escape(str(r['label']))}</td><td class=\"val\">{escape(_num(r['value']))} "
            f"{escape(str(r['unit']))}</td></tr>" for r in ff["readings"])
        extra += (f"<h2>Freeze frame — {escape(ff['dtc']['code'])}</h2>"
                  f"<p class=\"detail\">{escape(ff['dtc']['description'] or '')} · engine conditions when the "
                  f"fault was detected.</p><table><tbody>{ff_rows}</tbody></table>")
    if report.monitor_tests:
        mon_rows = "".join(
            f'<tr><td><span class="sev" style="background:{_STATUS[t["status"]][1]}">{_STATUS[t["status"]][0]}</span></td>'
            f"<td>{escape(t['monitor'])}</td><td>{escape(t['name'])}</td>"
            f"<td class=\"val\">{escape(_num(t['value']))} {escape(t['unit'])}</td>"
            f"<td class=\"val\">{escape(_num(t['min']))}</td><td class=\"val\">{escape(_num(t['max']))}</td></tr>"
            for t in report.monitor_tests)
        extra += ("<h2>On-board monitor tests (mode 06)</h2>"
                  "<table><thead><tr><th>Result</th><th>Monitor</th><th>Test</th><th>Value</th><th>Min</th>"
                  f"<th>Max</th></tr></thead><tbody>{mon_rows}</tbody></table>")
    counts = report.counts
    summary = " · ".join(f"{counts[k]} {_LABELS[k].lower()}" for k in ("critical", "warn", "info", "ok") if counts[k])
    grade_color = _GRADE.get(report.grade, "#333")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vehicle health report</title>
<style>
  body {{ font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #15191e; background: #fff;
         max-width: 900px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 6px; }}
  .head {{ display: flex; gap: 24px; align-items: center; flex-wrap: wrap; margin: 16px 0 20px; }}
  .score {{ width: 110px; height: 110px; border-radius: 50%; border: 8px solid {grade_color}; display: grid;
           place-items: center; text-align: center; flex: none; }}
  .score b {{ font-size: 34px; line-height: 1; display: block; }}
  .score span {{ color: {grade_color}; font-weight: 700; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; margin: 0; }}
  dt {{ color: #5d6673; }} dd {{ margin: 0; font-family: ui-monospace, Menlo, Consolas, monospace; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px; border-bottom: 1px solid #dde1e6; vertical-align: top; }}
  th {{ color: #5d6673; font-weight: 500; }}
  .sev {{ color: #fff; border-radius: 4px; padding: 1px 6px; font-size: 12px; white-space: nowrap; }}
  .detail {{ color: #5d6673; margin-top: 2px; }}
  .val {{ font-family: ui-monospace, Menlo, Consolas, monospace; white-space: nowrap; }}
  .note {{ color: #5d6673; font-size: 12px; margin-top: 20px; }}
  @media (max-width: 600px) {{ .val {{ white-space: normal; }} }}
</style></head>
<body>
<h1>Vehicle health report</h1>
<div class="muted">{escape(summary)}</div>
<div class="head">
  <div class="score"><div><b>{report.score}</b><span>{escape(report.grade)}</span></div></div>
  <dl>{meta_html}</dl>
</div>
<table><thead><tr><th>Result</th><th>Area</th><th>Finding</th><th>Value</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{extra}
<p class="note">{escape(report.disclaimer)} Generated by OBD Tool.</p>
</body></html>
"""
