"""Project links and the list of supported adapters shown in the UI and the README."""

from __future__ import annotations

AUTHOR = "Piotr Skwarna"
LICENSE = "GPL-2.0-or-later"
COPYRIGHT = f"Copyright (C) 2026 {AUTHOR}"
CONTACT_EMAIL = "piotr.skwarna@gmail.com"  # supporter keys and other private matters
KEY_REQUEST_URL = f"mailto:{CONTACT_EMAIL}?subject=OBD%20Tool%20supporter%20key"

REPO_URL = "https://github.com/spidero/obdruid"
ISSUES_URL = REPO_URL + "/issues"

# Ways to support the author: [{"name": "GitHub Sponsors", "url": "https://..."}].
# Shown in the "About & support" window; empty = the donation section is hidden.
SUPPORT_LINKS: list[dict] = [
    {"name": "GitHub Sponsors", "url": "https://github.com/sponsors/spidero"},
    {"name": "PayPal", "url": "https://paypal.me/spidero"},
]

# status: tested | should work | not supported
ADAPTERS = [
    {"name": "Vgate vLinker FS (USB)", "status": "tested",
     "notes": "FTDI serial, ELM327 v2.3 compatible, 115200 baud. Reference adapter."},
    {"name": "Other USB ELM327 / STN adapters (OBDLink SX/EX, vLinker FD/MC USB)", "status": "should work",
     "notes": "Anything that shows up as a serial port and speaks the ELM327 command set."},
    {"name": "Bluetooth ELM327 (classic and dual-mode, e.g. \"OBDII\")", "status": "tested",
     "notes": "Port rfcomm://MAC – no pairing tool or rfcomm bind needed (Linux). Tested with the blue "
              "\"ELM 327 MINI Interface\" (v2.1 clone): about 7 PID/s. Find it with: obdruid ports --scan."},
    {"name": "Bluetooth LE ELM327 (e.g. \"OBDBLE\", vLinker MC+, iOS-style adapters)", "status": "tested",
     "notes": "Port ble://MAC (Linux). Tested with a dual-mode ELM327 v2.1 clone: about 6 PID/s."},
    {"name": "Cheap ELM327 v1.5 / v2.1 clones", "status": "should work",
     "notes": "Often unreliable or slow; try the --slow option if the connection fails."},
    {"name": "Wi-Fi ELM327 adapters", "status": "should work",
     "notes": "Port socket://IP:PORT, usually socket://192.168.0.10:35000 after joining the adapter's Wi-Fi. Not tested yet."},
]

VEHICLES_NOTE = ("Any car with a standard OBD-II / EOBD socket (petrol from about 2001, diesel from about 2004 "
                 "in the EU). Tested on ISO 15765-4 CAN 11 bit / 500 kbit. Only the engine ECU's generic "
                 "emissions data is read - no ABS, airbag or gearbox modules.")
