"""Bluetooth transports, tested without Bluetooth: fake sockets bridge to the ELM327 simulator."""

import os
import queue
import socket
import struct
import threading
import time

import pytest
from serial import SerialException

from obdruid.core import ConnectionSettings, ObdSession, transports
from obdruid.core.simulator import Elm327Emulator
from obdruid.core.transports import protocol_ble, protocol_rfcomm
from obdruid.core.transports._base import StreamSerial


# ---------------------------------------------------------------- helpers
class PtyBridge:
    """Byte pipe to a running simulator: write() sends to the ELM327, chunks() yields its answers."""

    def __init__(self):
        self.emu = Elm327Emulator(stored_dtcs=("P0133",), pending_dtcs=()).start()
        self.fd = os.open(self.emu.port, os.O_RDWR | os.O_NOCTTY)
        self.out = queue.Queue()
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.alive:
            try:
                data = os.read(self.fd, 256)
            except OSError:
                break
            if data:
                self.out.put(data)

    def write(self, data):
        os.write(self.fd, data)

    def close(self):
        self.alive = False
        self.emu.stop()
        try:
            os.close(self.fd)
        except OSError:
            pass


class FakeRfcommSocket:
    """Stands in for an AF_BLUETOOTH/RFCOMM socket; only `good_channel` accepts the connection."""

    good_channel = 2

    def __init__(self, *args):
        self.bridge = None

    def settimeout(self, t):
        pass

    def connect(self, addr):
        mac, channel = addr
        if channel != self.good_channel:
            raise ConnectionRefusedError(111, "Connection refused")
        self.bridge = PtyBridge()

    def recv(self, n):
        try:
            return self.bridge.out.get(timeout=30)
        except queue.Empty:
            return b""

    def sendall(self, data):
        self.bridge.write(data)

    def shutdown(self, how):
        pass

    def close(self):
        if self.bridge:
            self.bridge.close()


class FakeAttSocket:
    """A tiny GATT server like a cheap BLE ELM327: FFF1 notify (value 0x15, CCCD 0x16), FFF2 write (0x18)."""

    NOTIFY, WRITE = 0x15, 0x18

    def __init__(self):
        self.bridge = PtyBridge()
        self.inbox = queue.Queue()     # PDUs the "adapter" sends to us
        self.notifying = False
        threading.Thread(target=self._notify_loop, daemon=True).start()

    def _decl(self, handle, props, value_handle, uuid):
        return struct.pack("<HBHH", handle, props, value_handle, uuid)

    def send(self, pdu):
        op = pdu[0]
        if op == 0x08:  # read by type: characteristic declarations
            start = struct.unpack_from("<H", pdu, 1)[0]
            decls = [(0x02, 0x0A, 0x03, 0x2A00), (0x14, 0x12, self.NOTIFY, 0xFFF1), (0x17, 0x0C, self.WRITE, 0xFFF2)]
            found = [d for d in decls if d[0] >= start]
            if found:
                self.inbox.put(bytes([0x09, 7]) + b"".join(self._decl(*d) for d in found))
            else:
                self.inbox.put(struct.pack("<BBHB", 0x01, 0x08, start, 0x0A))
        elif op == 0x12:  # write request (enable notifications)
            handle = struct.unpack_from("<H", pdu, 1)[0]
            self.notifying = handle == self.NOTIFY + 1
            self.inbox.put(b"\x13")
        elif op == 0x52:  # write command to FFF2
            assert struct.unpack_from("<H", pdu, 1)[0] == self.WRITE and len(pdu) - 3 <= 20
            self.bridge.write(pdu[3:])
        return len(pdu)

    def _notify_loop(self):
        while True:
            data = self.bridge.out.get()
            for i in range(0, len(data), 20):  # 20-byte notifications, as over a real LE link
                self.inbox.put(struct.pack("<BH", 0x1B, self.NOTIFY) + data[i:i + 20])

    def recv(self, n):
        try:
            return self.inbox.get(timeout=30)
        except queue.Empty:
            return b""

    def settimeout(self, t):
        pass

    def shutdown(self, how):
        pass

    def close(self):
        self.bridge.close()


# ---------------------------------------------------------------- unit tests
def test_url_parsing():
    assert protocol_rfcomm.parse_url("rfcomm://01:23:45:67:89:ba") == ("01:23:45:67:89:BA", list(range(1, 7)))
    assert protocol_rfcomm.parse_url("rfcomm://01:23:45:67:89:BA/3") == ("01:23:45:67:89:BA", [3])
    assert protocol_ble.parse_url("ble://81:23:45:67:89:BA?type=random") == ("81:23:45:67:89:BA", [2])
    assert protocol_ble.parse_url("ble://81:23:45:67:89:BA")[1] == [1, 2]
    with pytest.raises(ValueError):
        protocol_rfcomm.parse_url("rfcomm://not-a-mac")
    assert transports.is_url("ble://x") and transports.is_url("socket://1.2.3.4:35000")
    assert not transports.is_url("/dev/ttyUSB0") and not transports.is_url(None)


def test_pick_channel():
    full = lambda u: struct.pack("<H", u) + b""  # noqa: E731 - 16-bit UUID
    base128 = lambda u: bytes.fromhex("fb349b5f800000800010000000") [:12] + struct.pack("<H", u) + b"\0\0"  # noqa
    chars = [(0x02, 3, full(0x2A00)), (0x12, 0x15, full(0xFFF1)), (0x0C, 0x18, full(0xFFF2))]
    assert protocol_ble.pick_channel(chars) == (0x15, 0x18, False)  # write without response
    assert protocol_ble.pick_channel([(0x1A, 0x10, base128(0xFFE1))]) == (0x10, 0x10, True)  # one char, 128-bit form
    custom = [(0x02, 3, full(0x2A00)), (0x10, 0x30, os.urandom(16)), (0x08, 0x33, os.urandom(16))]
    assert protocol_ble.pick_channel(custom) == (0x30, 0x33, True)  # unknown vendor UUIDs, found by properties
    with pytest.raises(SerialException):
        protocol_ble.pick_channel([(0x02, 3, full(0x2A00)), (0x12, 0x11, full(0x2A19))])  # only standard chars


def test_classify_scan_results():
    ble = transports.classify("81:23:45:67:89:BA", "OBDBLE",
                              "\tUUID: Unknown (0000fff0-0000-1000-8000-00805f9b34fb)\n")
    assert (ble.kind, ble.url, ble.likely_obd) == ("ble", "ble://81:23:45:67:89:BA", True)
    classic = transports.classify("01:23:45:67:89:BA", "OBDII", "\tClass: 0x001f00\n")
    assert (classic.kind, classic.url, classic.likely_obd) == ("classic", "rfcomm://01:23:45:67:89:BA", True)
    tv = transports.classify("C4:8B:66:EF:61:B1", "GoogleTV3868", "\tClass: 0x000c0414\n")
    assert not tv.likely_obd and tv.kind == "classic"
    sensor = transports.classify("A4:C1:38:16:F6:18", "LYWSD03MMC", "\tAddressType: public\n")
    assert sensor.kind == "ble" and not sensor.likely_obd


def test_stream_serial_timeouts_and_loss():
    class Loop(StreamSerial):
        def _connect(self, url):
            self.sent = b""

        def _send(self, data):
            self.sent += data

        def _disconnect(self):
            pass

    s = Loop("loop://x", timeout=0.2)
    t = time.monotonic()
    assert s.read(5) == b"" and time.monotonic() - t >= 0.19   # times out like a serial port
    s.write(b"ATZ\r")
    s._feed(b"OK\r>")
    assert s.in_waiting == 4 and s.read(10) == b"OK\r>"
    s._lost()
    with pytest.raises(SerialException):
        s.read(1)
    s.close()


# ---------------------------------------------------------------- full program over the fake links
def test_session_over_rfcomm(monkeypatch):
    monkeypatch.setattr(protocol_rfcomm.socket, "AF_BLUETOOTH", 31, raising=False)
    monkeypatch.setattr(protocol_rfcomm.socket, "BTPROTO_RFCOMM", 3, raising=False)
    monkeypatch.setattr(protocol_rfcomm.socket, "socket", FakeRfcommSocket)
    with ObdSession(ConnectionSettings(port="rfcomm://01:23:45:67:89:BA")) as s:
        assert s.connected
        rpm = {r.key: r.value for r in s.read(["RPM", "COOLANT_TEMP"])}
        assert rpm["RPM"] > 0
        assert [d.code for d in s.read_dtcs().stored] == ["P0133"]


def test_session_over_ble(monkeypatch):
    monkeypatch.setattr(protocol_ble.sys, "platform", "linux")
    monkeypatch.setattr(protocol_ble, "att_connect", lambda mac, addr_type: FakeAttSocket())
    with ObdSession(ConnectionSettings(port="ble://81:23:45:67:89:BA")) as s:
        assert s.connected
        assert {r.key: r.value for r in s.read(["RPM"])}["RPM"] > 0
        assert s.vin() == "SIM1234567890ABCD"  # multi-frame answer split over several notifications


def test_unreachable_adapter_message(monkeypatch):
    class Refuse(FakeRfcommSocket):
        def connect(self, addr):
            raise OSError(113, "No route to host")

    monkeypatch.setattr(protocol_rfcomm.socket, "AF_BLUETOOTH", 31, raising=False)
    monkeypatch.setattr(protocol_rfcomm.socket, "BTPROTO_RFCOMM", 3, raising=False)
    monkeypatch.setattr(protocol_rfcomm.socket, "socket", Refuse)
    transports.register()
    import serial
    with pytest.raises(SerialException, match="No route to host"):
        serial.serial_for_url("rfcomm://01:23:45:67:89:BA", timeout=1)
