"""ELM327 emulator on a pseudo terminal, so the whole stack runs without a car.

The emulator mimics a vLinker FS (ELM327 v2.3) connected to a petrol car on
ISO 15765-4 CAN 11 bit / 500 kbit with a single engine ECU at 7E8 - the same
set-up as the test car. It answers AT commands, mode 01 (live data), 03/07
(trouble codes), 04 (clear codes) and 09 02 (VIN).

Linux/macOS only (uses os.openpty).
"""

from __future__ import annotations

import math
import os
import random
import select
import threading
import time
import tty

from . import dtc as dtc_codes

ECU_RESPONSE_ID = "7E8"
ELM_ID = "ELM327 v2.3"


class EngineModel:
    """Very small time based model of a running engine (120 s drive cycle)."""

    def __init__(self, seed: int | None = None):
        self.start = time.monotonic()
        self.rand = random.Random(seed)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def _phase(self) -> tuple[float, float]:
        """Return (speed km/h, throttle 0..1) for the current point of the cycle."""
        t = self.elapsed() % 120
        if t < 20:
            return 0.0, 0.0
        if t < 50:
            k = (t - 20) / 30
            return 80 * k, 0.35 + 0.1 * math.sin(t)
        if t < 90:
            return 80 + 3 * math.sin(t / 3), 0.18
        if t < 110:
            k = (t - 90) / 20
            return 80 * (1 - k), 0.0
        return 0.0, 0.0

    def noise(self, amp: float) -> float:
        return self.rand.uniform(-amp, amp)

    @property
    def speed(self) -> float:
        return max(0.0, self._phase()[0])

    @property
    def throttle(self) -> float:
        return self._phase()[1]

    @property
    def rpm(self) -> float:
        speed = self.speed
        if speed < 1:
            base = 680 + 25 * math.sin(self.elapsed())
        else:
            gear_ratio = 2600 / 50 if speed < 50 else 2300 / 80
            base = max(900, speed * gear_ratio)
        return base + 1200 * self.throttle + self.noise(8)

    @property
    def coolant(self) -> float:
        return 90 - 65 * math.exp(-self.elapsed() / 180) + self.noise(0.3)

    @property
    def load(self) -> float:
        return min(100.0, 18 + 70 * self.throttle + self.noise(1))

    @property
    def map_kpa(self) -> float:
        return 30 + 65 * self.throttle + self.noise(1)

    @property
    def maf(self) -> float:
        return self.rpm * self.map_kpa / 9000 + self.noise(0.05)


class Elm327Emulator:
    """Threaded ELM327 emulator bound to a pseudo terminal."""

    def __init__(self, stored_dtcs=("P0133", "P0420"), pending_dtcs=("P0171",),
                 vin="SIM1234567890ABCD", seed: int | None = None):
        self.engine = EngineModel(seed)
        self.stored_dtcs = list(stored_dtcs)
        self.pending_dtcs = list(pending_dtcs)
        self.vin = vin
        self.distance_with_mil = 42 if self.stored_dtcs else 0
        self.distance_since_clear = 1234
        self.warmups_since_clear = 37
        self.cleared_at: float | None = None
        self.commands_seen: list[str] = []
        self._reset()
        self._master, self._slave = os.openpty()
        tty.setraw(self._slave)
        self.port = os.ttyname(self._slave)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="elm327-emulator", daemon=True)

    # ------------------------------------------------------------------ life
    def start(self) -> "Elm327Emulator":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for fd in (self._master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------ state
    def _reset(self) -> None:
        self.echo = True
        self.headers = False
        self.linefeeds = False
        self.spaces = True
        self.protocol_found = False
        self.last_command = ""

    # ------------------------------------------------------------------ io loop
    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._master], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(self._master, 1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                self._handle(line.decode(errors="replace"))

    def _write(self, text: str) -> None:
        try:
            os.write(self._master, text.encode())
        except OSError:
            pass

    def _handle(self, raw: str) -> None:
        command = raw.strip().replace(" ", "").upper()
        if command == "":
            command = self.last_command  # empty CR repeats the last command
        else:
            self.last_command = command
        self.commands_seen.append(command)

        echo = raw + "\r" if self.echo else ""
        lines = self._respond(command)
        eol = "\r\n" if self.linefeeds else "\r"
        out = echo + "".join(line + eol for line in lines) + eol + ">"
        self._write(out)

    # ------------------------------------------------------------------ commands
    def _respond(self, cmd: str) -> list[str]:
        if not cmd:
            return []
        if "\x7f" in cmd:
            return ["?"]
        if cmd.startswith("AT"):
            return self._at(cmd[2:])
        if cmd.startswith("ST"):
            return ["vLinker FS r2 (sim)"] if cmd == "STDI" else ["?"]
        if not all(c in "0123456789ABCDEF" for c in cmd):
            return ["?"]
        if len(cmd) % 2 == 1 and len(cmd) > 2:
            cmd = cmd[:-1]  # python-OBD "fast" mode appends the expected frame count
        if not self.protocol_found:
            self.protocol_found = True
            frames = self._obd(cmd)
            return ["SEARCHING..."] + frames
        return self._obd(cmd)

    def _at(self, arg: str) -> list[str]:
        if arg == "Z":
            self._reset()
            return ["", ELM_ID]
        if arg in ("I",):
            return [ELM_ID]
        if arg == "@1":
            return ["OBDII to RS232 Interpreter (sim)"]
        if arg == "RV":
            return [f"{14.1 + self.engine.noise(0.1):.1f}V"]
        if arg == "DP":
            return ["AUTO, ISO 15765-4 (CAN 11/500)"]
        if arg == "DPN":
            return ["A6"]
        if arg in ("E0", "E1"):
            self.echo = arg == "E1"
            return ["OK"]
        if arg in ("H0", "H1"):
            self.headers = arg == "H1"
            return ["OK"]
        if arg in ("L0", "L1"):
            self.linefeeds = arg == "L1"
            return ["OK"]
        if arg in ("S0", "S1"):
            self.spaces = arg == "S1"
            return ["OK"]
        if arg.startswith(("SP", "TP")):
            return ["OK"]
        return ["OK"]

    def _obd(self, cmd: str) -> list[str]:
        mode = int(cmd[0:2], 16)
        if mode == 0x01 and len(cmd) == 4:
            data = self._mode01(int(cmd[2:4], 16))
        elif mode == 0x03 and len(cmd) == 2:
            data = self._dtc_frame(0x43, self.stored_dtcs)
        elif mode == 0x07 and len(cmd) == 2:
            data = self._dtc_frame(0x47, self.pending_dtcs)
        elif mode == 0x04 and len(cmd) == 2:
            self._clear()
            data = bytes([0x44])
        elif mode == 0x09 and len(cmd) == 4:
            data = self._mode09(int(cmd[2:4], 16))
        else:
            data = None
        if data is None:
            return ["NO DATA"]
        return self._format(data)

    # ------------------------------------------------------------------ framing
    def _hex(self, data: bytes) -> str:
        sep = " " if self.spaces else ""
        return sep.join(f"{b:02X}" for b in data)

    def _format(self, data: bytes) -> list[str]:
        """Format a response payload as ISO-TP frames the way an ELM327 prints them."""
        sep = " " if self.spaces else ""
        if len(data) <= 7:
            if self.headers:
                return [sep.join([ECU_RESPONSE_ID, f"{len(data):02X}", self._hex(data)])]
            return [self._hex(data)]
        chunks = [data[:6]] + [data[i:i + 7] for i in range(6, len(data), 7)]
        if self.headers:
            lines = [sep.join([ECU_RESPONSE_ID, "10", f"{len(data):02X}", self._hex(chunks[0])])]
            for n, chunk in enumerate(chunks[1:], start=1):
                padded = chunk + bytes(7 - len(chunk))
                lines.append(sep.join([ECU_RESPONSE_ID, f"2{n % 16:X}", self._hex(padded)]))
            return lines
        lines = [f"{len(data):03X}"]
        for n, chunk in enumerate(chunks):
            lines.append(f"{n % 16:X}:{sep}{self._hex(chunk)}")
        return lines

    # ------------------------------------------------------------------ mode 01
    def _supported_mask(self, base: int, supported: set[int]) -> bytes:
        bits = 0
        for pid in range(base + 1, base + 0x21):
            if pid in supported or (pid == base + 0x20 and any(p > base + 0x20 for p in supported)):
                bits |= 1 << (32 - (pid - base))
        return bits.to_bytes(4, "big")

    def _status_bytes(self) -> bytes:
        a = (0x80 if self.stored_dtcs else 0x00) | min(len(self.stored_dtcs), 0x7F)
        b = 0x07  # spark ignition; misfire, fuel and components monitors supported
        c = 0xE1  # catalyst, O2 sensor, O2 heater, EGR supported
        d = 0x00  # all complete
        if self.cleared_at is not None:
            # after clearing, monitors become ready one by one (one every 20 s)
            done = int((time.monotonic() - self.cleared_at) // 20)
            pending = [bit for bit in (0x01, 0x20, 0x40, 0x80) if c & bit][done:]
            for bit in pending:
                d |= bit
            if done < 1:
                b |= 0x20  # fuel system not ready yet
        return bytes([a, b, c, d])

    def _live_values(self) -> dict[int, bytes]:
        e = self.engine
        rpm = max(0, int(e.rpm * 4))
        trim_s = 128 + int(e.noise(4))
        o2 = 0.45 + 0.4 * math.sin(e.elapsed() * 3)
        return {
            0x01: self._status_bytes(),
            0x03: bytes([0x02, 0x00]),
            0x04: bytes([int(e.load * 255 / 100) & 0xFF]),
            0x05: bytes([int(e.coolant + 40)]),
            0x06: bytes([trim_s]),
            0x07: bytes([131]),
            0x0B: bytes([int(e.map_kpa)]),
            0x0C: rpm.to_bytes(2, "big"),
            0x0D: bytes([int(e.speed)]),
            0x0E: bytes([int((8 + 20 * e.throttle + e.noise(1) + 64) * 2)]),
            0x0F: bytes([int(22 + 40)]),
            0x10: int(e.maf * 100).to_bytes(2, "big"),
            0x11: bytes([int((12 + 70 * e.throttle) * 255 / 100)]),
            0x13: bytes([0x03]),
            0x14: bytes([int(o2 * 200), trim_s]),
            0x15: bytes([int(0.7 * 200), 0xFF]),
            0x1C: bytes([0x06]),  # EOBD
            0x1F: int(e.elapsed()).to_bytes(2, "big"),
            0x21: self.distance_with_mil.to_bytes(2, "big"),
            0x30: bytes([self.warmups_since_clear]),
            0x31: self.distance_since_clear.to_bytes(2, "big"),
            0x33: bytes([99]),
            0x3C: int((420 + 200 * e.throttle + 40) * 10).to_bytes(2, "big"),
            0x42: int((14.1 + e.noise(0.05)) * 1000).to_bytes(2, "big"),
            0x43: int(e.load * 255 / 100 * 0.9).to_bytes(2, "big"),
            0x44: int(1.0 * 32768).to_bytes(2, "big"),
            0x45: bytes([int(70 * e.throttle * 255 / 100)]),
            0x46: bytes([int(12 + 40)]),
            0x47: bytes([int((14 + 70 * e.throttle) * 255 / 100)]),
            0x49: bytes([int((15 + 80 * e.throttle) * 255 / 100)]),
            0x4A: bytes([int((7 + 40 * e.throttle) * 255 / 100)]),
            0x4C: bytes([int((5 + 70 * e.throttle) * 255 / 100)]),
        }

    def _mode01(self, pid: int) -> bytes | None:
        values = self._live_values()
        supported = set(values)
        if pid in (0x00, 0x20, 0x40):
            if pid != 0x00 and not any(p > pid for p in supported):
                return None
            return bytes([0x41, pid]) + self._supported_mask(pid, supported)
        if pid not in values:
            return None
        return bytes([0x41, pid]) + values[pid]

    # ------------------------------------------------------------------ DTC / mode 09
    def _dtc_frame(self, sid: int, codes: list[str]) -> bytes:
        payload = b"".join(dtc_codes.encode(c) for c in codes)
        return bytes([sid, len(codes)]) + payload

    def _clear(self) -> None:
        self.stored_dtcs.clear()
        self.pending_dtcs.clear()
        self.distance_with_mil = 0
        self.distance_since_clear = 0
        self.warmups_since_clear = 0
        self.cleared_at = time.monotonic()

    def _mode09(self, pid: int) -> bytes | None:
        if pid == 0x00:
            return bytes([0x49, 0x00, 0x40, 0x00, 0x00, 0x00])  # only 0902 (VIN)
        if pid == 0x02 and self.vin:
            return bytes([0x49, 0x02, 0x01]) + self.vin.encode().ljust(17, b" ")[:17]
        return None
