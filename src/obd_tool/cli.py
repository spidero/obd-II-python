"""Command line interface: obd-tool info | live | dtc | clear | pids | ports | sim | web | gui."""

from __future__ import annotations

import csv
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import __version__
from .core import DEFAULT_DASHBOARD, ConnectionSettings, ObdError, ObdSession, find_ports
from .core.simulator import FAULTS

app = typer.Typer(
    help="OBD-II workshop tool: live data, reading and clearing trouble codes.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)


def _settings(ctx: typer.Context) -> ConnectionSettings:
    return ctx.obj


def _open(ctx: typer.Context) -> ObdSession:
    s = _settings(ctx)
    where = "simulator" if s.simulate else (s.port or "auto detect")
    with console.status(f"Connecting to the adapter ({where})..."):
        session = ObdSession(s)
        session.connect()
    return session


def _fail(e: Exception) -> None:
    err.print(f"[bold red]Error:[/] {e}")
    raise typer.Exit(2)


def _version(value: bool):
    if value:
        console.print(f"obd-tool {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    port: Optional[str] = typer.Option(None, "--port", "-p", envvar="OBD_PORT",
                                       help="Adapter port, e.g. /dev/ttyUSB0 (default: auto detect)."),
    baud: Optional[int] = typer.Option(None, "--baud", "-b", envvar="OBD_BAUD",
                                       help="Serial baud rate (default: auto detect)."),
    protocol: Optional[str] = typer.Option(None, "--protocol", envvar="OBD_PROTOCOL",
                                           help="ELM327 protocol number, e.g. 6 = CAN 11/500 (default: auto)."),
    sim: bool = typer.Option(False, "--sim", envvar="OBD_SIM",
                             help="Use the built-in simulator instead of a real car."),
    sim_fault: Optional[List[str]] = typer.Option(None, "--sim-fault", envvar="OBD_SIM_FAULTS",
                                                  help="Inject a simulator fault (repeatable): "
                                                       + ", ".join(FAULTS) + "."),
    sim_no_codes: bool = typer.Option(False, "--sim-no-codes", help="Simulator starts without trouble codes."),
    slow: bool = typer.Option(False, "--slow", help="Disable python-OBD fast mode (for picky adapters)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show communication logs."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version and exit."),
):
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
        import obd

        obd.logger.setLevel(logging.DEBUG)
    faults = tuple(f.strip() for item in (sim_fault or []) for f in item.split(",") if f.strip())
    unknown = sorted(set(faults) - set(FAULTS))
    if unknown:
        err.print(f"[bold red]Error:[/] unknown simulator fault(s): {', '.join(unknown)}")
        raise typer.Exit(2)
    ctx.obj = ConnectionSettings(port=port, baudrate=baud, protocol=protocol, fast=not slow, simulate=sim,
                                 sim_faults=faults, sim_codes=not sim_no_codes)


@app.command()
def ports():
    """List serial ports (OBD adapters)."""
    found = find_ports()
    if not found:
        console.print("No serial ports found.")
        raise typer.Exit(1)
    for p in found:
        console.print(p)


@app.command()
def info(ctx: typer.Context):
    """Adapter, protocol, VIN, MIL and readiness monitors."""
    try:
        with _open(ctx) as s:
            adapter = s.adapter_info()
            vin = s.vin()
            status = s.status()
    except ObdError as e:
        _fail(e)

    t = Table(show_header=False, box=None)
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Port", adapter.port + (" (simulator)" if adapter.simulated else ""))
    t.add_row("Adapter", adapter.elm_version or "?")
    t.add_row("Voltage", f"{adapter.voltage:.1f} V" if adapter.voltage is not None else "?")
    t.add_row("Protocol", f"{adapter.protocol} (#{adapter.protocol_id})")
    t.add_row("VIN", vin or "[dim]not reported by the car[/]")
    t.add_row("Ignition", status.ignition_type)
    t.add_row("MIL (check engine)", "[bold red]ON[/]" if status.mil else "[green]off[/]")
    t.add_row("Stored codes", str(status.dtc_count))
    console.print(t)

    r = Table(title="Readiness monitors")
    r.add_column("Monitor")
    r.add_column("State")
    for test in status.readiness:
        r.add_row(test.label, "[green]complete[/]" if test.complete else "[yellow]incomplete[/]")
    console.print(r)


@app.command()
def pids(ctx: typer.Context):
    """Live parameters supported by the car."""
    try:
        with _open(ctx) as s:
            params = s.supported_params()
    except ObdError as e:
        _fail(e)
    t = Table(title=f"Supported parameters ({len(params)})")
    for col in ("PID", "Key", "Name", "Unit", "Group"):
        t.add_column(col)
    for p in params:
        t.add_row(p.pid, p.key, p.label, p.unit, p.group)
    console.print(t)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


@app.command()
def live(
    ctx: typer.Context,
    keys: Optional[List[str]] = typer.Argument(None, help="Parameters (keys from `obd-tool pids`). Default: the basic set."),
    interval: float = typer.Option(0.5, "--interval", "-i", help="Interval between reads [s]."),
    count: int = typer.Option(0, "--count", "-n", help="Number of reads (0 = until Ctrl+C)."),
    csv_path: Optional[Path] = typer.Option(None, "--csv", help="Log readings to a CSV file."),
):
    """Live data (Ctrl+C to stop)."""
    try:
        session = _open(ctx)
    except ObdError as e:
        _fail(e)
    writer = None
    csv_file = None
    try:
        supported = {p.key for p in session.supported_params()}
        if keys:
            keys = [k.upper() for k in keys]
        else:
            keys = [k for k in DEFAULT_DASHBOARD if k in supported]
        unsupported = [k for k in keys if k not in supported]
        if unsupported:
            err.print(f"[yellow]Not supported by the car:[/] {', '.join(unsupported)}")

        if csv_path:
            csv_file = csv_path.open("w", newline="", encoding="utf-8")
            writer = csv.writer(csv_file)
            writer.writerow(["time"] + keys)

        def render(readings, n):
            t = Table(title=f"Live data  ·  read {n}  ·  {datetime.now():%H:%M:%S}")
            t.add_column("Parameter")
            t.add_column("Value", justify="right", style="bold cyan")
            t.add_column("Unit")
            for r in readings:
                t.add_row(r.label, _fmt(r.value), r.unit)
            return t

        n = 0
        with Live(console=console, auto_refresh=False) as view:
            while count == 0 or n < count:
                started = time.monotonic()
                readings = session.read(keys)
                n += 1
                view.update(render(readings, n), refresh=True)
                if writer:
                    writer.writerow([datetime.now().isoformat(timespec="milliseconds")]
                                    + [r.value for r in readings])
                    csv_file.flush()
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        pass
    except ObdError as e:
        _fail(e)
    finally:
        session.close()
        if csv_file:
            csv_file.close()
            console.print(f"Saved: {csv_path}")


def _print_dtcs(report) -> None:
    if not report.total:
        console.print("[green]No trouble codes[/] (stored or pending).")
        return
    for title, items in (("Stored (confirmed)", report.stored), ("Pending", report.pending)):
        if not items:
            continue
        t = Table(title=title)
        t.add_column("Code", style="bold red")
        t.add_column("Description")
        t.add_column("System")
        for d in items:
            t.add_row(d.code, d.description or "[dim]no description (manufacturer code?)[/]", d.system)
        console.print(t)


@app.command()
def dtc(ctx: typer.Context):
    """Read trouble codes (stored and pending)."""
    try:
        with _open(ctx) as s:
            status = s.status()
            report = s.read_dtcs()
    except ObdError as e:
        _fail(e)
    console.print("MIL (check engine): " + ("[bold red]ON[/]" if status.mil else "[green]off[/]"))
    _print_dtcs(report)


CLEAR_WARNING = (
    "Clearing codes (mode 04) erases stored and pending codes and freeze frame data,\n"
    "and resets the readiness monitors. Until the monitors complete again (a few drive\n"
    "cycles) the car may fail an emissions inspection."
)


@app.command()
def clear(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
):
    """Clear trouble codes (asks for confirmation)."""
    try:
        with _open(ctx) as s:
            report = s.read_dtcs()
            _print_dtcs(report)
            console.print(f"[yellow]{CLEAR_WARNING}[/]")
            if not yes and not typer.confirm("Clear trouble codes?", default=False):
                console.print("Cancelled.")
                raise typer.Exit(1)
            s.clear_dtcs()
            after = s.read_dtcs()
    except ObdError as e:
        _fail(e)
    if after.total:
        console.print("[yellow]Codes are still present after clearing (the fault is still there):[/]")
        _print_dtcs(after)
    else:
        console.print("[green]Trouble codes cleared.[/]")


SEVERITY_STYLE = {
    "critical": ("[bold white on red] CRITICAL [/]", "red"),
    "warn": ("[bold black on yellow] WARNING [/]", "yellow"),
    "info": ("[bold white on blue] INFO [/]", "blue"),
    "ok": ("[bold black on green] OK [/]", "green"),
    "skipped": ("[dim] SKIPPED [/]", "dim"),
}


@app.command()
def health(
    ctx: typer.Context,
    duration: float = typer.Option(30.0, "--duration", "-d", min=3, help="Sampling time [s]."),
    json_out: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    html: Optional[Path] = typer.Option(None, "--html", help="Save the report as an HTML file."),
):
    """Health check: sample live data and rate the car's condition (engine running and warm)."""
    import json as jsonlib

    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn

    from .core.health import run_check
    from .core.report_html import render_html

    try:
        session = _open(ctx)
    except ObdError as e:
        _fail(e)
    try:
        with Progress(TextColumn("{task.description}"), BarColumn(), TimeRemainingColumn(),
                      console=err, transient=True, disable=json_out) as bar:
            task = bar.add_task("Starting", total=1.0)
            report = run_check(session, duration,
                               lambda f, msg: bar.update(task, completed=f, description=msg))
    except ObdError as e:
        _fail(e)
    finally:
        session.close()

    if html:
        html.write_text(render_html(report), encoding="utf-8")
    if json_out:
        console.print_json(jsonlib.dumps(report.to_dict()))
        return

    color = {"Good": "green", "Fair": "yellow", "Poor": "red"}[report.grade]
    console.print(f"\n[bold]Vehicle health:[/] [bold {color}]{report.score}/100 – {report.grade}[/]")
    t = Table(show_header=True, box=None, padding=(0, 1))
    t.add_column("Result")
    t.add_column("Area", style="dim")
    t.add_column("Finding")
    t.add_column("Value", justify="right")
    for f in report.findings:
        badge, style = SEVERITY_STYLE[f.severity]
        text = f"[bold]{f.title}[/]" + (f"\n[dim]{f.detail}[/]" if f.detail else "")
        t.add_row(badge, f.area, text, f"[{style}]{f.value}[/]")
    console.print(t)
    console.print(f"[dim]{report.disclaimer}[/]")
    if html:
        console.print(f"Saved: {html}")


@app.command()
def sim():
    """Run only the adapter simulator and print its port (for testing other programs)."""
    from .core.simulator import Elm327Emulator

    with Elm327Emulator() as emu:
        console.print(f"ELM327 simulator running on: [bold]{emu.port}[/]  (Ctrl+C to quit)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


@app.command()
def web(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", envvar="OBD_WEB_HOST",
                             help="Listen address (0.0.0.0 = whole network)."),
    port: int = typer.Option(8090, "--web-port", envvar="OBD_WEB_PORT", help="Web server port."),
    no_connect: bool = typer.Option(False, "--no-connect", help="Do not connect to the adapter on start."),
):
    """Start the web interface."""
    from .web.server import run

    run(_settings(ctx), host=host, port=port, connect=not no_connect)


@app.command()
def gui(ctx: typer.Context):
    """Start the desktop app (the web interface in a window)."""
    from .gui import run

    run(_settings(ctx))


if __name__ == "__main__":  # pragma: no cover
    app()
