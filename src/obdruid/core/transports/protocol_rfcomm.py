"""pyserial handler for rfcomm://AA:BB:CC:DD:EE:FF[/channel] – classic Bluetooth SPP adapters."""

from __future__ import annotations

import socket
import threading

from serial import SerialException

from . import parse_mac
from ._base import StreamSerial


AUTO_CHANNELS = range(1, 7)  # cheap adapters move their SPP channel around; a wrong one is refused at once


def parse_url(url: str) -> tuple[str, list[int]]:
    rest = url.split("://", 1)[1].strip("/")
    mac, _, channel = rest.partition("/")
    return parse_mac(mac), [int(channel)] if channel else list(AUTO_CHANNELS)


class Serial(StreamSerial):
    def _connect(self, url):
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise SerialException("Classic Bluetooth sockets are not available on this system; "
                                  "pair the adapter and use its serial port (COMx / /dev/rfcommX) instead.")
        try:
            mac, channels = parse_url(url)
        except ValueError as e:
            raise SerialException(str(e)) from e
        sock, last = None, None
        for channel in channels:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            sock.settimeout(20)
            try:
                sock.connect((mac, channel))
                break
            except ConnectionRefusedError as e:  # no SPP service on this channel: try the next one
                last = e
            except OSError as e:                 # adapter unreachable: no point trying other channels
                last = e
                sock.close()
                sock = None
                break
            sock.close()
            sock = None
        if sock is None:
            raise SerialException(f"Bluetooth connection to {mac} failed: {last}. Is the adapter in the OBD socket, "
                                  "the ignition on and the adapter not connected to a phone?")
        self.channel = channel
        sock.settimeout(None)
        self._sock = sock
        self._reader = threading.Thread(target=self._read_loop, name="obd-rfcomm", daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            while True:
                data = self._sock.recv(1024)
                if not data:
                    break
                self._feed(data)
        except OSError:
            pass
        self._lost()

    def _send(self, data):
        try:
            self._sock.sendall(data)
        except OSError as e:
            raise SerialException(f"Bluetooth write failed: {e}") from e

    def _disconnect(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
