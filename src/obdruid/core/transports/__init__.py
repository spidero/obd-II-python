"""Extra adapter transports for python-OBD (which opens ports with pyserial's serial_for_url).

URLs handled here:
  rfcomm://AA:BB:CC:DD:EE:FF[/channel]   classic Bluetooth (SPP) over an RFCOMM socket – no pairing tool,
                                         no `rfcomm bind`, no root
  ble://AA:BB:CC:DD:EE:FF[?type=random]  Bluetooth Low Energy (Linux): ATT over an LE L2CAP socket
pyserial itself handles socket://host:port (Wi-Fi adapters).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

URL_SCHEMES = ("rfcomm://", "ble://", "socket://")
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
# names cheap and branded adapters advertise
OBD_NAME_RE = re.compile(r"obd|elm|v-?link|vgate|obdlink|konnwei|icar|carista|veepeak|scan", re.I)
# GATT services typical for ELM327 BLE adapters
OBD_BLE_SERVICES = ("0000fff0", "0000ffe0", "000018f0", "e7810a71")


def register() -> None:
    """Make pyserial (and so python-OBD) understand rfcomm:// and ble:// ports."""
    import serial

    package = __name__
    if package not in serial.protocol_handler_packages:
        serial.protocol_handler_packages.append(package)


def is_url(port: str | None) -> bool:
    return bool(port) and port.lower().startswith(URL_SCHEMES)


def parse_mac(text: str) -> str:
    mac = text.strip().upper()
    if not MAC_RE.match(mac):
        raise ValueError(f"Not a Bluetooth address: {text!r} (expected AA:BB:CC:DD:EE:FF)")
    return mac


@dataclass
class BluetoothDevice:
    address: str
    name: str
    kind: str        # "ble" or "classic"
    url: str
    likely_obd: bool

    def to_dict(self) -> dict:
        return asdict(self)


def classify(address: str, name: str, info: str) -> BluetoothDevice:
    """Decide from `bluetoothctl info` output whether a device is BLE or classic and if it looks like OBD."""
    low = info.lower()
    # classic devices report a Bluetooth class of device; LE-only ones do not
    obd_service = any(service in low for service in OBD_BLE_SERVICES)
    ble = obd_service or "ble" in name.lower() or "class:" not in low
    kind = "ble" if ble else "classic"
    url = f"ble://{address}" if ble else f"rfcomm://{address}"
    likely = bool(OBD_NAME_RE.search(name)) or obd_service
    return BluetoothDevice(address=address, name=name, kind=kind, url=url, likely_obd=likely)


def scan_bluetooth(seconds: int = 10) -> list[BluetoothDevice]:
    """Discover nearby Bluetooth devices (Linux, BlueZ `bluetoothctl`); OBD-looking ones first."""
    if not sys.platform.startswith("linux") or not shutil.which("bluetoothctl"):
        return []
    subprocess.run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"],
                   capture_output=True, text=True, timeout=seconds + 15)
    listing = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=15).stdout
    found = []
    for line in listing.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 2 or parts[0] != "Device" or not MAC_RE.match(parts[1]):
            continue
        address, name = parts[1], parts[2] if len(parts) > 2 else ""
        if not name or name.replace("-", ":").upper() == address:
            continue  # nameless devices (BlueZ shows the address instead) are never adapters to pick
        info = subprocess.run(["bluetoothctl", "info", address], capture_output=True, text=True, timeout=15).stdout
        found.append(classify(address, name, info))
    return sorted(found, key=lambda d: (not d.likely_obd, d.name.lower()))
