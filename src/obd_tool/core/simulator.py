"""ELM327 emulator on a pseudo terminal, so the whole stack runs without a car.

The emulator mimics a vLinker FS (ELM327 v2.3) connected to a petrol car on
ISO 15765-4 CAN 11 bit / 500 kbit with a single engine ECU at 7E8 - the same
set-up as the test car. It answers AT commands, mode 01 (live data), 02 (freeze
frame), 03/07/0A (stored, pending and permanent trouble codes), 04 (clear codes),
06 (on-board monitor test results) and 09 02 (VIN).

Faults can be injected to see what a bad health check looks like, see FAULTS.

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

# Injectable faults: name -> description
FAULTS = {
    "lean": "mixture too lean (fuel trims about +22 %)",
    "rich": "mixture too rich (fuel trims about -22 %)",
    "charging": "alternator not charging (12.4 V)",
    "overheat": "engine overheating (110 °C)",
    "rough_idle": "unstable idle (misfire-like rpm swings)",
    "lazy_o2": "slow upstream O2 sensor",
    "catalyst": "worn catalyst (downstream O2 follows upstream)",
    "cleared": "codes cleared recently (monitors incomplete)",
}
ELM_ID = "ELM327 v2.3"


class EngineModel:
    """Very small time based model of a running engine (120 s drive cycle)."""

    def __init__(self, seed: int | None = None, offset: float = 0.0, faults: set[str] | None = None):
        self.start = time.monotonic() - offset  # offset: seconds the engine has already been running
        self.rand = random.Random(seed)
        self.faults = faults or set()

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
            if "rough_idle" in self.faults:
                base += self.noise(140)
        else:
            gear_ratio = 2600 / 50 if speed < 50 else 2300 / 80
            base = max(900, speed * gear_ratio)
        return base + 1200 * self.throttle + self.noise(8)

    @property
    def coolant(self) -> float:
        if "overheat" in self.faults:
            return 110 + self.noise(0.5)
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
                 vin="SIM1234567890ABCD", seed: int | None = None, faults=(), warm: bool = True):
        unknown = set(faults) - set(FAULTS)
        if unknown:
            raise ValueError(f"Unknown simulator fault(s): {', '.join(sorted(unknown))}")
        self.faults = set(faults)
        self.engine = EngineModel(seed, offset=600 if warm else 0, faults=self.faults)
        self.stored_dtcs = list(stored_dtcs)
        self.pending_dtcs = list(pending_dtcs)
        # permanent codes mirror the confirmed ones and survive clearing (mode 04)
        self.permanent_dtcs = list(self.stored_dtcs[-1:])
        # the freeze frame belongs to the first confirmed code and is erased by clearing
        self.freeze_frame: dict[int, bytes] | None = self._snapshot(self.stored_dtcs[0]) if self.stored_dtcs else None
        self.vin = vin
        self.distance_with_mil = 42 if self.stored_dtcs else 0
        self.distance_since_clear = 1234
        self.warmups_since_clear = 37
        self.cleared_at: float | None = None
        if "cleared" in self.faults:
            self.distance_since_clear, self.warmups_since_clear = 12, 2
            self.cleared_at = time.monotonic() + 3600  # monitors stay incomplete
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
            return [f"{self._voltage() + self.engine.noise(0.1):.1f}V"]
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
        elif mode == 0x02 and len(cmd) == 6:
            data = self._mode02(int(cmd[2:4], 16), int(cmd[4:6], 16))
        elif mode == 0x06 and len(cmd) == 4:
            data = self._mode06(int(cmd[2:4], 16))
        elif mode == 0x0A and len(cmd) == 2:
            data = self._dtc_frame(0x4A, self.permanent_dtcs)
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
            done = max(0, int((time.monotonic() - self.cleared_at) // 20))
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
        trim_l = 131
        if "lean" in self.faults:
            trim_s, trim_l = trim_s + 15, 141
        elif "rich" in self.faults:
            trim_s, trim_l = trim_s - 15, 115
        speed_o2 = 0.4 if "lazy_o2" in self.faults else 3
        o2 = 0.45 + 0.4 * math.sin(e.elapsed() * speed_o2)
        o2_down = 0.45 + 0.35 * math.sin(e.elapsed() * speed_o2 - 0.5) if "catalyst" in self.faults \
            else 0.7 + e.noise(0.02)
        return {
            0x01: self._status_bytes(),
            0x03: bytes([0x02, 0x00]),
            0x04: bytes([int(e.load * 255 / 100) & 0xFF]),
            0x05: bytes([int(e.coolant + 40)]),
            0x06: bytes([trim_s]),
            0x07: bytes([trim_l]),
            0x0B: bytes([int(e.map_kpa)]),
            0x0C: rpm.to_bytes(2, "big"),
            0x0D: bytes([int(e.speed)]),
            0x0E: bytes([int((8 + 20 * e.throttle + e.noise(1) + 64) * 2)]),
            0x0F: bytes([int(22 + 40)]),
            0x10: int(e.maf * 100).to_bytes(2, "big"),
            0x11: bytes([int((12 + 70 * e.throttle) * 255 / 100)]),
            0x13: bytes([0x03]),
            0x14: bytes([int(o2 * 200), trim_s]),
            0x15: bytes([int(o2_down * 200), 0xFF]),
            0x1C: bytes([0x06]),  # EOBD
            0x1F: int(e.elapsed()).to_bytes(2, "big"),
            0x21: self.distance_with_mil.to_bytes(2, "big"),
            0x30: bytes([self.warmups_since_clear]),
            0x31: self.distance_since_clear.to_bytes(2, "big"),
            0x33: bytes([99]),
            0x3C: int((420 + 200 * e.throttle + 40) * 10).to_bytes(2, "big"),
            0x42: int((self._voltage() + e.noise(0.05)) * 1000).to_bytes(2, "big"),
            0x43: int(e.load * 255 / 100 * 0.9).to_bytes(2, "big"),
            0x44: int(1.0 * 32768).to_bytes(2, "big"),
            0x45: bytes([int(70 * e.throttle * 255 / 100)]),
            0x46: bytes([int(12 + 40)]),
            0x47: bytes([int((14 + 70 * e.throttle) * 255 / 100)]),
            0x49: bytes([int((15 + 80 * e.throttle) * 255 / 100)]),
            0x4A: bytes([int((7 + 40 * e.throttle) * 255 / 100)]),
            0x4C: bytes([int((5 + 70 * e.throttle) * 255 / 100)]),
        }

    def _voltage(self) -> float:
        return 12.4 if "charging" in self.faults else 14.1

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

    # ------------------------------------------------------------------ mode 02
    def _snapshot(self, code: str) -> dict[int, bytes]:
        """Freeze frame: the car was cruising at about 78 km/h when the fault was confirmed."""
        lean = "lean" in self.faults
        return {
            0x02: dtc_codes.encode(code),
            0x03: bytes([0x02, 0x00]),                      # closed loop
            0x04: bytes([int(46 * 255 / 100)]),             # load 46 %
            0x05: bytes([88 + 40]),                         # coolant 88 °C
            0x06: bytes([128 + (18 if lean else 4)]),       # short term trim
            0x07: bytes([128 + (13 if lean else 3)]),       # long term trim
            0x0B: bytes([52]),                              # MAP 52 kPa
            0x0C: (2350 * 4).to_bytes(2, "big"),            # 2350 rpm
            0x0D: bytes([78]),                              # 78 km/h
            0x0E: bytes([(14 + 64) * 2]),                   # timing advance 14°
            0x0F: bytes([24 + 40]),                         # intake air 24 °C
            0x10: int(13.6 * 100).to_bytes(2, "big"),       # MAF 13.6 g/s
            0x11: bytes([int(24 * 255 / 100)]),             # throttle 24 %
            0x1F: (812).to_bytes(2, "big"),                 # 812 s since engine start
        }

    def _mode02(self, pid: int, frame: int) -> bytes | None:
        if frame != 0:
            return None
        head = bytes([0x42, pid, frame])
        stored = self.freeze_frame or {}
        if pid == 0x02:
            return head + stored.get(0x02, b"\x00\x00")  # 0000 = no freeze frame stored
        if pid == 0x00:
            return head + self._supported_mask(0x00, set(stored) | {0x02})
        if pid not in stored:
            return None
        return head + stored[pid]

    # ------------------------------------------------------------------ mode 06
    def _monitors(self) -> dict[int, list[tuple[int, int, int, int, int]]]:
        """Monitor id -> [(test id, unit/scaling id, value, min, max)] as raw 16 bit numbers."""
        if self.cleared_at is not None and "cleared" not in self.faults \
                and time.monotonic() - self.cleared_at < 20:
            return {mid: [] for mid in (0x01, 0x02, 0x21, 0x31, 0x41, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)}
        slow_o2 = "lazy_o2" in self.faults or "P0133" in self.stored_dtcs
        worn_cat = "catalyst" in self.faults or "P0420" in self.stored_dtcs
        rough = "rough_idle" in self.faults
        ratio = lambda x: int(x * 256)  # noqa: E731 - unit 0x20: 1/256 per bit
        return {
            # upstream O2 sensor: switch times in ms (unit 0x10), limit 150 ms
            0x01: [(0x05, 0x10, 168 if slow_o2 else 62, 0, 150), (0x06, 0x10, 141 if slow_o2 else 55, 0, 150)],
            # downstream O2 sensor: maximum voltage must reach 0.6 V (unit 0x0B: mV)
            0x02: [(0x08, 0x0B, 782, 600, 0xFFFF)],
            # catalyst: oxygen storage ratio, limit 0.60
            0x21: [(0x80, 0x20, ratio(0.66 if worn_cat else 0.27), 0, ratio(0.60))],
            # EGR: pressure change must be at least 1.5 kPa (unit 0x17: 0.01 kPa)
            0x31: [(0x81, 0x17, 420, 150, 0xFFFF)],
            # O2 heater current 0.4-2.0 A (unit 0x0E: mA)
            0x41: [(0x82, 0x0E, 1020, 400, 2000)],
            # misfire counters (unit 0x24: counts)
            0xA1: [(0x0B, 0x24, 3 if rough else 0, 0, 0xFFFF), (0x0C, 0x24, 21 if rough else 0, 0, 0xFFFF)],
            0xA2: [(0x0B, 0x24, 0, 0, 0xFFFF), (0x0C, 0x24, 0, 0, 0xFFFF)],
            0xA3: [(0x0B, 0x24, 0, 0, 0xFFFF), (0x0C, 0x24, 0, 0, 0xFFFF)],
            0xA4: [(0x0B, 0x24, 3 if rough else 0, 0, 0xFFFF), (0x0C, 0x24, 21 if rough else 0, 0, 0xFFFF)],
            0xA5: [(0x0B, 0x24, 0, 0, 0xFFFF), (0x0C, 0x24, 0, 0, 0xFFFF)],
        }

    def _mode06(self, mid: int) -> bytes | None:
        monitors = self._monitors()
        if mid % 0x20 == 0:
            if mid and not any(m > mid for m in monitors):
                return None
            return bytes([0x46, mid]) + self._supported_mask(mid, set(monitors))
        if mid not in monitors:
            return None
        out = bytes([0x46])
        for tid, uas, value, lo, hi in monitors[mid] or [(0x0C if mid >= 0xA1 else 0x80, 0x24, 0, 0, 0)]:
            out += bytes([mid, tid, uas]) + value.to_bytes(2, "big") + lo.to_bytes(2, "big") + hi.to_bytes(2, "big")
        return out

    # ------------------------------------------------------------------ DTC / mode 09
    def _dtc_frame(self, sid: int, codes: list[str]) -> bytes:
        payload = b"".join(dtc_codes.encode(c) for c in codes)
        return bytes([sid, len(codes)]) + payload

    def _clear(self) -> None:
        self.stored_dtcs.clear()
        self.pending_dtcs.clear()
        self.freeze_frame = None
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
