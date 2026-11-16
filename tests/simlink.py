"""Raw byte link to a running simulator, over whichever transport it uses (pty or TCP)."""

from __future__ import annotations

import os
import select
import socket


class SimLink:
    def __init__(self, emu, raw: bool = False):
        self.fd = self.sock = None
        if emu.transport == "tcp":
            host, port = emu.port.removeprefix("socket://").rsplit(":", 1)
            self.sock = socket.create_connection((host, int(port)))
        else:
            self.fd = os.open(emu.port, os.O_RDWR | os.O_NOCTTY)
            if raw:
                import tty

                tty.setraw(self.fd)

    def write(self, data: bytes) -> None:
        if self.sock is not None:
            self.sock.sendall(data)
        else:
            os.write(self.fd, data)

    def read(self, size: int = 1024, timeout: float | None = None) -> bytes:
        """Next chunk of bytes; b"" on timeout or when the link is closed."""
        source = self.sock if self.sock is not None else self.fd
        if timeout is not None and not select.select([source], [], [], timeout)[0]:
            return b""
        try:
            return self.sock.recv(size) if self.sock is not None else os.read(self.fd, size)
        except OSError:
            return b""

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
            else:
                os.close(self.fd)
        except OSError:
            pass
