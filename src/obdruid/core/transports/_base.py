"""Common pyserial-compatible base for socket-like transports."""

from __future__ import annotations

import threading
import time

from serial import SerialBase, SerialException
from serial.serialutil import PortNotOpenError


class StreamSerial(SerialBase):
    """A pyserial `Serial` look-alike over a byte stream; subclasses implement _connect/_send/_disconnect
    and push received bytes with _feed() (from a reader thread)."""

    def open(self):
        if self._port is None:
            raise SerialException("Port must be configured before it can be used.")
        if self.is_open:
            raise SerialException("Port is already open.")
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._connect(self._port)
        self.is_open = True

    def _reconfigure_port(self, force_update=False):
        pass  # baud rate, parity... mean nothing for a wireless link

    def close(self):
        if self.is_open:
            self.is_open = False
            self._closed = True
            with self._cond:
                self._cond.notify_all()
            self._disconnect()

    def _feed(self, data: bytes) -> None:
        with self._cond:
            self._buffer.extend(data)
            self._cond.notify_all()

    def _lost(self) -> None:
        self._closed = True
        with self._cond:
            self._cond.notify_all()

    @property
    def in_waiting(self) -> int:
        if not self.is_open:
            raise PortNotOpenError()
        return len(self._buffer)

    def read(self, size=1):
        if not self.is_open:
            raise PortNotOpenError()
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        with self._cond:
            while len(self._buffer) < size and not self._closed:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    break
                self._cond.wait(left)
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
        if not data and self._closed and self.is_open:
            raise SerialException("Connection to the adapter was lost")
        return data

    def write(self, data):
        if not self.is_open:
            raise PortNotOpenError()
        if self._closed:
            raise SerialException("Connection to the adapter was lost")
        data = bytes(data)
        self._send(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self._cond:
            self._buffer.clear()

    def reset_output_buffer(self):
        pass

    # subclasses
    def _connect(self, url: str) -> None:
        raise NotImplementedError

    def _send(self, data: bytes) -> None:
        raise NotImplementedError

    def _disconnect(self) -> None:
        raise NotImplementedError
