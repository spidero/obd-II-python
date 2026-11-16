# Changelog

## Unreleased

### Added
- **PID sets for more makes:** VW group (VW, Audi, Seat, Škoda, Cupra), Toyota, Hyundai / Kia and Renault / Dacia –
  engine and gearbox parameters (80 per set) merged from the community database OBDb, with Polish labels.
  Unverified: switch a set on and probing keeps what your car answers.
- **`tools/obdb_convert.py`:** converts OBDb signal sets of any make into an OBDruid PID set (bit positions to
  formulas, imperial to metric units, the most common parameters first); `--fetch Mazda` downloads the data.

## 0.6.1 – 2026-11-16

### Added
- **A window for the Linux AppImage without making it bigger:** it uses what the system has – GTK with WebKit
  of the system Python, or Chrome / Chromium / Edge / Brave / Vivaldi as an app window (no tabs, no address bar),
  and only then a browser tab. The program quits when the window is closed. `OBDRUID_WINDOW=gtk|chromium|browser`
  chooses one. Also used by `obdruid gui` when pywebview is not installed.

### Fixed
- The AppImage starts the system browser with a clean environment (the libraries of the build are not passed on).

## 0.6.0 – 2026-11-16

Manufacturer parameters and ready-to-run builds for Windows and Linux.

### Added
- **Manufacturer parameters (PID sets):** values read with the car maker's own identifiers (UDS service 22),
  described in TOML files – formula, unit, module address, gauge limits. Sets are switched on per car, probed
  once and then shown like any other parameter (dashboard, CSV, `obdruid live`). Read only by design.
  New card in *Vehicle info*, `obdruid did` for a single raw read, a demo set in the simulator
  (`--sim-fault dpf_regen`, `dpf_full`).
- First sets for real cars: **Ford diesel – particulate filter** and **Hyundai i30 (2007–2012) petrol – engine**, candidates converted from OBDb (CC BY-SA 4.0),
  marked unverified until confirmed on a car. Service 21 data blocks (Hyundai/Kia, Toyota) can be read too.
- **Ready-to-run builds without Python:** a Windows zip (`OBDruid.exe` + `obdruid-cli.exe`) and Linux AppImages
  for x86_64 and aarch64, built and smoke-tested by CI and attached to every release.
- Windows support in the core: the simulator works without a pseudo terminal, data lives in `%APPDATA%\OBDruid`,
  the test suite also runs on Windows.
- The desktop app quits by itself when it runs in the browser and its tab is closed.

### Changed
- Python 3.11 or newer is required (3.10 reached its end of life); CI runs on 3.11–3.14 and on Windows.

### Fixed
- Windows: the command line no longer crashes when its output is redirected (legacy code page); the trip
  computer counts time between quick reads (coarse system clock).

### Known limitations
- Manufacturer parameter sets for real cars are unverified candidates – confirm the values before relying on them.
- The Windows build was tested by CI against the simulator only; a real adapter on a COM port and the window
  (WebView2) still need a test on a real PC. The builds are not code-signed (SmartScreen warning).

## 0.5.0 – 2026-11-15

First public release under the name **OBDruid** (formerly OBD Tool).

### Added
- **Bluetooth and Wi-Fi adapters:** `rfcomm://MAC` (classic, no pairing or `rfcomm bind`), `ble://MAC`
  (Bluetooth LE on Linux, also dual-mode adapters) and `socket://IP:PORT`; adapter search in the web UI and
  `obdruid ports --scan`.
- **Battery and alternator test:** battery at rest, voltage dip while starting, charging at idle and under
  load, with plain-language findings (`obdruid battery` and the Health check tab).
- **Car switcher** in the top bar: each vehicle profile remembers its adapter; cars that report no VIN are
  recognised by the adapter they use.
- **Automatic reconnect** with back-off after a lost link; lost "supported PIDs" answers are asked again.
- **Polish trouble code descriptions** for all ~1800 generic codes.
- **VIN decoding** (manufacturer, country, model year) and profiles named from the VIN.
- **Installable web app (PWA)** and optional HTTPS (`obdruid web --ssl-cert/--ssl-key`).
- Health report as PDF (print dialog), `obdruid gui --remote HOST` to show another machine's UI.
- Support window with supporter keys ("Licensed to"), logo, community files, CI on Python 3.10–3.13.

### Changed
- Package and command renamed to `obdruid` (`obd-tool` still works); data folder moved to
  `~/.local/share/obdruid` automatically. License: GPL-2.0-or-later.

### Fixed
- A used vehicle profile no longer adopts another car's VIN.
- Dashboard: charts and min/max are reset when the car changes; a missed reading keeps the last value.
- Mode 06: a value equal to its limit is a pass (no false "close to the limit" warning).

## 0.3.0 and earlier

Development versions: core, simulator, CLI, web UI, desktop GUI, health check, freeze frame, mode 06,
gauges and alarms, trip computer, vehicle history, Polish UI.
