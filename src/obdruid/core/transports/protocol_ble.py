"""pyserial handler for ble://AA:BB:CC:DD:EE:FF[?type=random] – Bluetooth Low Energy ELM327 adapters.

Linux only. Talks ATT directly over an LE L2CAP socket (like `gatttool`), which works without root and also
with dual-mode adapters that BlueZ's D-Bus API insists on connecting over classic Bluetooth.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import select
import socket
import struct
import sys
import threading
from urllib.parse import parse_qs

from serial import SerialException

from . import parse_mac
from ._base import StreamSerial

AF_BLUETOOTH, BTPROTO_L2CAP, ATT_CID = 31, 0, 4
BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM = 1, 2
ATT_MTU = 23            # default LE MTU: 20 bytes of payload per write / notification
UUID_CHAR_DECL = 0x2803

# Standard GAP/GATT/device-info/battery characteristics – never the data channel.
STANDARD_CHARS = {0x2A00, 0x2A01, 0x2A02, 0x2A03, 0x2A04, 0x2A05, 0x2A19, 0x2A23, 0x2A24, 0x2A25, 0x2A26,
                  0x2A27, 0x2A28, 0x2A29, 0x2A2A, 0x2A50, 0x2AA6, 0x2B29, 0x2B2A}
# Known data channels: (notify uuid, write uuid). Others are found by their properties.
KNOWN_PAIRS = [(0xFFF1, 0xFFF2), (0xFFE1, 0xFFE1), (0x2AF0, 0x2AF1), (0xFFE1, 0xFFE2)]

PROP_WRITE_NR, PROP_WRITE, PROP_NOTIFY, PROP_INDICATE = 0x04, 0x08, 0x10, 0x20


class _SockaddrL2(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("psm", ctypes.c_ushort), ("bdaddr", ctypes.c_ubyte * 6),
                ("cid", ctypes.c_ushort), ("bdaddr_type", ctypes.c_ubyte)]


def _sockaddr(mac: str, addr_type: int) -> _SockaddrL2:
    a = _SockaddrL2()
    a.family, a.cid, a.bdaddr_type = AF_BLUETOOTH, ATT_CID, addr_type
    for i, part in enumerate(reversed(mac.split(":"))):
        a.bdaddr[i] = int(part, 16)
    return a


def att_connect(mac: str, addr_type: int, timeout: float = 15.0) -> socket.socket:
    """Open an ATT channel to an LE device (raw L2CAP socket, fixed channel 4)."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    fd = libc.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET | socket.SOCK_NONBLOCK, BTPROTO_L2CAP)
    if fd < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    local, remote = _sockaddr("00:00:00:00:00:00", BDADDR_LE_PUBLIC), _sockaddr(mac, addr_type)
    if libc.bind(fd, ctypes.byref(local), ctypes.sizeof(local)) < 0:
        err = ctypes.get_errno()
        os.close(fd)
        raise OSError(err, os.strerror(err))
    if libc.connect(fd, ctypes.byref(remote), ctypes.sizeof(remote)) < 0:
        err = ctypes.get_errno()
        if err != 115:  # EINPROGRESS: wait for the link below
            os.close(fd)
            raise OSError(err, os.strerror(err))
    sock = socket.socket(fileno=fd)
    if not select.select([], [sock], [], timeout)[1]:
        sock.close()
        raise OSError(110, "Connection timed out")
    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if err:
        sock.close()
        raise OSError(err, os.strerror(err))
    sock.setblocking(True)
    return sock


def uuid16(raw: bytes) -> int | None:
    """16-bit form of a UUID (from a 16-bit or a Bluetooth-base 128-bit value), else None."""
    if len(raw) == 2:
        return struct.unpack("<H", raw)[0]
    base = bytes.fromhex("fb349b5f80000080001000000000")  # little-endian tail of 0000xxxx-0000-1000-8000-00805f9b34fb
    if len(raw) == 16 and raw[:12] == base[:12] and raw[14:] == b"\x00\x00":
        return struct.unpack_from("<H", raw, 12)[0]
    return None


def pick_channel(chars: list[tuple[int, int, bytes]]) -> tuple[int, int, bool]:
    """From (properties, value handle, uuid) tuples pick the notify handle, write handle and whether
    writes need a response. Known adapter UUIDs first, then any non-standard notify + write pair."""
    by_uuid = {uuid16(u): (p, h) for p, h, u in chars if uuid16(u) is not None}
    for n_uuid, w_uuid in KNOWN_PAIRS:
        if n_uuid in by_uuid and w_uuid in by_uuid:
            (np_, nh), (wp, wh) = by_uuid[n_uuid], by_uuid[w_uuid]
            if np_ & (PROP_NOTIFY | PROP_INDICATE) and wp & (PROP_WRITE | PROP_WRITE_NR):
                return nh, wh, not wp & PROP_WRITE_NR
    custom = [(p, h) for p, h, u in chars if uuid16(u) not in STANDARD_CHARS]
    notify = next((h for p, h in custom if p & (PROP_NOTIFY | PROP_INDICATE)), None)
    writer = next(((p, h) for p, h in custom if p & (PROP_WRITE | PROP_WRITE_NR)), None)
    if notify is None or writer is None:
        raise SerialException("This Bluetooth LE device has no serial-like data channel (not an OBD adapter?)")
    return notify, writer[1], not writer[0] & PROP_WRITE_NR


def parse_url(url: str) -> tuple[str, list[int]]:
    rest = url.split("://", 1)[1]
    mac, _, query = rest.strip("/").partition("?")
    kind = parse_qs(query).get("type", [""])[0].lower()
    types = {"public": [BDADDR_LE_PUBLIC], "random": [BDADDR_LE_RANDOM]}.get(kind,
                                                                             [BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM])
    return parse_mac(mac), types


class Serial(StreamSerial):
    def _connect(self, url):
        if not sys.platform.startswith("linux"):
            raise SerialException("Bluetooth LE adapters are supported on Linux only for now.")
        try:
            mac, types = parse_url(url)
        except ValueError as e:
            raise SerialException(str(e)) from e
        last = None
        for addr_type in types:
            try:
                self._sock = att_connect(mac, addr_type)
                break
            except OSError as e:
                last = e
        else:
            raise SerialException(f"Bluetooth LE connection to {mac} failed: {last}. Is the adapter in the OBD "
                                  "socket and not connected to a phone?")
        self._sock.settimeout(10)
        self._pending = None  # response opcode we wait for during discovery
        try:
            chars = self._discover()
            self._notify, self._write_handle, self._with_response = pick_channel(chars)
            self._request(struct.pack("<BHH", 0x12, self._notify + 1, 1), 0x13)  # enable notifications (CCCD)
        except (OSError, struct.error) as e:
            self._sock.close()
            raise SerialException(f"Bluetooth LE adapter {mac} did not answer the service discovery: {e}") from e
        self._sock.settimeout(None)
        self._write_lock = threading.Lock()
        self._acks = threading.Semaphore(0)
        self._reader = threading.Thread(target=self._read_loop, name="obd-ble", daemon=True)
        self._reader.start()

    # ---- ATT during discovery (synchronous)
    def _request(self, pdu: bytes, want: int) -> bytes:
        self._sock.send(pdu)
        while True:
            r = self._sock.recv(512)
            if r and r[0] in (want, 0x01):
                return r
            self._handle_unsolicited(r)

    def _discover(self) -> list[tuple[int, int, bytes]]:
        chars, start = [], 1
        while start <= 0xFFFF:
            r = self._request(struct.pack("<BHHH", 0x08, start, 0xFFFF, UUID_CHAR_DECL), 0x09)
            if r[0] == 0x01:  # error response: no more characteristics
                break
            length, last = r[1], start
            for i in range(2, len(r) - length + 1, length):
                handle, props, value_handle = struct.unpack_from("<HBH", r, i)
                chars.append((props, value_handle, bytes(r[i + 5:i + length])))
                last = handle
            start = last + 1
        return chars

    def _handle_unsolicited(self, pdu: bytes) -> None:
        if not pdu:
            return
        op = pdu[0]
        if op in (0x1B, 0x1D) and len(pdu) >= 3:          # notification / indication
            if struct.unpack_from("<H", pdu, 1)[0] == getattr(self, "_notify", None):
                self._feed(pdu[3:])
            if op == 0x1D:
                self._sock.send(b"\x1e")                    # confirm the indication
        elif op == 0x13 and hasattr(self, "_acks"):         # write response
            self._acks.release()
        elif op == 0x02:                                    # MTU request from the adapter
            self._sock.send(struct.pack("<BH", 0x03, ATT_MTU))
        elif op & 0x01 == 0 and op not in (0x52, 0xD2):     # any other request: not supported
            self._sock.send(struct.pack("<BBHB", 0x01, op, 0, 0x06))

    # ---- running
    def _read_loop(self):
        try:
            while True:
                pdu = self._sock.recv(512)
                if not pdu:
                    break
                self._handle_unsolicited(pdu)
        except OSError:
            pass
        self._lost()

    def _send(self, data):
        size = ATT_MTU - 3
        with self._write_lock:
            for i in range(0, len(data), size):
                chunk = data[i:i + size]
                try:
                    if self._with_response:
                        self._sock.send(struct.pack("<BH", 0x12, self._write_handle) + chunk)
                        if not self._acks.acquire(timeout=5):
                            raise SerialException("Bluetooth LE write was not confirmed")
                    else:
                        self._sock.send(struct.pack("<BH", 0x52, self._write_handle) + chunk)
                except OSError as e:
                    raise SerialException(f"Bluetooth LE write failed: {e}") from e

    def _disconnect(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
