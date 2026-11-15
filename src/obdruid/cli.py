"""Command line interface: obdruid info | live | dtc | tests | clear | health | history | pids | ports | sim | web | gui."""

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

from . import __version__, about
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


def _storage():
    from .core.storage import Storage

    return Storage()


def _reader(session: ObdSession):
    """Live reader with the trip computer set to the active vehicle's fuel."""
    from .core.trip import LiveReader, TripComputer

    vehicle = _select_vehicle(session)
    return LiveReader(session, TripComputer(fuel=vehicle.get("fuel", "petrol") if vehicle else "petrol"))


def _select_vehicle(session: ObdSession):
    """Activate the profile of the connected car (the simulator has its own). None if storage fails."""
    try:
        return _storage().select_for_vin(session.vin(), simulated=session.simulated)
    except (OSError, ObdError):
        return None


def _version(value: bool):
    if value:
        console.print(f"obdruid {__version__}\n{about.COPYRIGHT}, license {about.LICENSE}")
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
def ports(
    scan: bool = typer.Option(False, "--scan", "-s", help="Also search for Bluetooth adapters (Linux)."),
    seconds: int = typer.Option(10, "--seconds", help="How long to search for Bluetooth devices."),
):
    """List serial ports and, with --scan, nearby Bluetooth OBD adapters."""
    from .core import transports

    found = find_ports()
    for p in found:
        console.print(p)
    if not found and not scan:
        console.print("No serial ports found. Bluetooth adapter? Try: obdruid ports --scan")
        raise typer.Exit(1)
    if scan:
        console.print(f"Searching for Bluetooth devices ({seconds} s)...")
        devices = transports.scan_bluetooth(seconds)
        if not devices:
            console.print("No Bluetooth devices found (is Bluetooth on and the adapter powered?).")
            return
        t = Table("Name", "Type", "Use as --port", "OBD?")
        for d in devices:
            t.add_row(d.name or "?", "BLE" if d.kind == "ble" else "classic", d.url, "yes" if d.likely_obd else "")
        console.print(t)


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
            params = _reader(s).params()
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
    keys: Optional[List[str]] = typer.Argument(None, help="Parameters (keys from `obdruid pids`). Default: the basic set."),
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
    reader = _reader(session)
    try:
        supported = {p.key for p in reader.params()}
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
                readings = reader.read(keys)
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
        trip = reader.trip.summary()
        if (trip["distance_km"] or 0) >= 0.1:
            avg = trip["average_l_100km"]
            console.print(f"Trip: {trip['distance_km']:.1f} km in {trip['duration_s'] / 60:.0f} min"
                          + (f", {trip['fuel_l']:.2f} l, {avg:.1f} l/100 km" if avg is not None else ""))


def _print_dtcs(report) -> None:
    if not report.total:
        console.print("[green]No trouble codes[/] (stored or pending).")
    for title, items in (("Stored (confirmed)", report.stored), ("Pending", report.pending),
                         ("Permanent (cannot be cleared, go away once the fault is proven fixed)", report.permanent)):
        if not items:
            continue
        t = Table(title=title)
        t.add_column("Code", style="bold red")
        t.add_column("Description")
        t.add_column("System")
        for d in items:
            t.add_row(d.code, d.description or "[dim]no description (manufacturer code?)[/]", d.system)
        console.print(t)


def _print_freeze(frame) -> None:
    if not frame.supported:
        console.print("[dim]Freeze frame: not supported by the car.[/]")
        return
    if not frame.present:
        console.print("[dim]Freeze frame: none stored.[/]")
        return
    t = Table(title=f"Freeze frame for {frame.dtc.code} – engine conditions when the fault was detected")
    t.add_column("Parameter")
    t.add_column("Value", justify="right", style="bold cyan")
    t.add_column("Unit")
    for r in frame.readings:
        t.add_row(r.label, _fmt(r.value), r.unit)
    console.print(t)


@app.command()
def dtc(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Print the codes and the freeze frame as JSON."),
):
    """Read trouble codes (stored, pending, permanent) and the freeze frame."""
    import json as jsonlib

    try:
        with _open(ctx) as s:
            status = s.status()
            report = s.read_dtcs()
            frame = s.read_freeze_frame()
    except ObdError as e:
        _fail(e)
    if json_out:
        data = report.to_dict()
        data.update(mil=status.mil, freeze_frame=frame.to_dict())
        console.print_json(jsonlib.dumps(data))
        return
    console.print("MIL (check engine): " + ("[bold red]ON[/]" if status.mil else "[green]off[/]"))
    _print_dtcs(report)
    _print_freeze(frame)


TEST_STYLE = {"pass": "[green]pass[/]", "near": "[bold yellow]near limit[/]", "fail": "[bold red]FAIL[/]"}


@app.command()
def tests(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Print the results as JSON."),
):
    """On-board monitor test results (mode 06): measured values against the ECU's limits."""
    import json as jsonlib

    try:
        with _open(ctx) as s:
            report = s.read_monitors()
    except ObdError as e:
        _fail(e)
    if json_out:
        console.print_json(jsonlib.dumps(report.to_dict()))
        return
    if not report.supported:
        console.print("The car does not report monitor test results (mode 06 needs a CAN protocol).")
        raise typer.Exit(1)
    if not report.tests:
        console.print("No test results yet – the monitors have not run since the codes were cleared.")
        return
    t = Table(title=f"Monitor tests ({len(report.tests)})")
    for col, kw in (("Monitor", {}), ("Test", {}), ("Value", {"justify": "right"}), ("Min", {"justify": "right"}),
                    ("Max", {"justify": "right"}), ("Unit", {}), ("Headroom", {"justify": "right"}), ("Result", {})):
        t.add_column(col, **kw)
    for x in report.tests:
        t.add_row(x.monitor, x.name, _fmt(x.value), _fmt(x.min), _fmt(x.max), x.unit,
                  f"{x.margin:.0f} %" if x.margin is not None else "—", TEST_STYLE[x.status])
    console.print(t)
    failed, near = report.count("fail"), report.count("near")
    if failed or near:
        console.print(f"[yellow]{failed} failed, {near} close to the limit.[/]")


CLEAR_WARNING = (
    "Clearing codes (mode 04) erases stored and pending codes and freeze frame data,\n"
    "and resets the readiness monitors and monitor test results. Until the monitors\n"
    "complete again (a few drive cycles) the car may fail an emissions inspection.\n"
    "The codes and the freeze frame are saved to the history first."
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
            from .web.server import backup_before_clear

            try:
                storage = _storage()
                storage.select_for_vin(s.vin(), simulated=s.simulated)
                backup = backup_before_clear(s, storage)
                if backup:
                    console.print(f"Backup saved: {storage.record_path(storage.active()['id'], backup['id'])}")
            except OSError as e:
                err.print(f"[yellow]Could not save a backup of the codes:[/] {e}")
                if not yes and not typer.confirm("Clear anyway?", default=False):
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
    save: bool = typer.Option(True, "--save/--no-save", help="Keep the report in the vehicle history."),
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
        if save:
            _select_vehicle(session)
    except ObdError as e:
        _fail(e)
    finally:
        session.close()

    if html:
        html.write_text(render_html(report), encoding="utf-8")
    saved = None
    if save:
        try:
            storage = _storage()
            from .web.server import _health_summary

            data = report.to_dict()
            saved = storage.add_record(storage.active()["id"], "health", _health_summary(data), data)
        except OSError as e:
            err.print(f"[yellow]Could not save the report to the history:[/] {e}")
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
    if saved:
        console.print(f"[dim]Saved to the history as {saved['id']} (see `obdruid history`).[/]")


@app.command()
def history(
    record: Optional[str] = typer.Argument(None, help="Record id to print as JSON (default: list the records)."),
    kind: Optional[str] = typer.Option(None, "--kind", "-k", help="Only this kind: health, dtc-backup or trip."),
):
    """Saved health reports, pre-clear backups and trips of the active vehicle."""
    import json as jsonlib

    from .core.storage import StorageError

    storage = _storage()
    vehicle = storage.active()
    if record:
        try:
            console.print_json(jsonlib.dumps(storage.record(vehicle["id"], record)))
        except StorageError as e:
            _fail(e)
        return
    records = storage.records(vehicle["id"], kind)
    console.print(f"Vehicle: [bold]{vehicle['name']}[/]" + (f" (VIN {vehicle['vin']})" if vehicle.get("vin") else "")
                  + f"  ·  {storage.root}")
    if not records:
        console.print("No records yet.")
        return
    t = Table()
    for col in ("Id", "Date", "Kind", "Summary"):
        t.add_column(col)
    for r in records:
        t.add_row(r["id"], r["created"].replace("T", " "), r["kind"], r["summary"])
    console.print(t)


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
def supporter(
    key: Optional[str] = typer.Argument(None, help="Supporter key to activate (OBDT1-...)."),
    remove: bool = typer.Option(False, "--remove", help="Remove the activated key."),
):
    """Show or activate the supporter key that hides the "Support the project" window."""
    from .core import supporter as sup
    from .core.storage import default_data_dir

    root = default_data_dir()
    if remove:
        sup.remove(root)
        console.print("Supporter key removed.")
        return
    if key:
        try:
            active = sup.save(root, key)
        except sup.InvalidKey as e:
            err.print(f"[red]{e}[/]")
            raise typer.Exit(1)
        console.print(f"[green]Thank you![/] Licensed to: {active.name}")
        return
    active = sup.load(root)
    if active:
        console.print(f"Licensed to: {active.name} (key {active.id}, issued {active.issued})")
    else:
        console.print("No supporter key. Support the project: " + ", ".join(l["url"] for l in about.SUPPORT_LINKS))
        console.print(f"then write to {about.CONTACT_EMAIL} with the payment confirmation and the name to show; "
                      "the key comes by e-mail.")


@app.command()
def gui(
    ctx: typer.Context,
    remote: Optional[str] = typer.Option(
        None, "--remote", "-r", envvar="OBD_REMOTE",
        help="Show the web UI of another machine instead, e.g. a Pi with the adapter: "
             "raspberrypi.local or http://192.168.1.50:8090 (port 8090 is the default)."),
):
    """Start the desktop app (the web interface in a window)."""
    from .gui import RemoteError, run, run_remote

    if remote:
        try:
            run_remote(remote)
        except RemoteError as e:
            err.print(f"[red]{e}[/]")
            raise typer.Exit(1)
        return
    run(_settings(ctx))


if __name__ == "__main__":  # pragma: no cover
    app()
