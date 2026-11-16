# Changelog

## Unreleased

### Added
- **Manufacturer parameters (PID sets):** values read with the car maker's own identifiers (UDS service 22),
  described in TOML files – formula, unit, module address, gauge limits. Sets are switched on per car, probed
  once and then shown like any other parameter (dashboard, CSV, `obdruid live`). Read only by design.
  New card in *Vehicle info*, `obdruid did` for a single raw read, a demo set in the simulator
  (`--sim-fault dpf_regen`, `dpf_full`).
- First sets for real cars: **Ford diesel – particulate filter** and **Hyundai i30 (2007–2012) petrol – engine**, candidates converted from OBDb (CC BY-SA 4.0),
  marked unverified until confirmed on a car. Service 21 data blocks (Hyundai/Kia, Toyota) can be read too.

### Changed
- Python 3.11 or newer is required (3.10 reached its end of life); CI runs on 3.11–3.14.

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
