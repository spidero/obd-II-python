import os
import select
import termios
import tty

from obd_tool.core.simulator import Elm327Emulator


def talk(fd, cmd, timeout=3.0):
    os.write(fd, (cmd + "\r").encode())
    buf = b""
    while b">" not in buf:
        ready, _, _ = select.select([fd], [], [], timeout)
        assert ready, f"timeout waiting for answer to {cmd!r}, got {buf!r}"
        buf += os.read(fd, 1024)
    return [line for line in buf.decode().replace(">", "").split("\r") if line]


def test_raw_dialogue():
    with Elm327Emulator(stored_dtcs=["P0133", "P0420", "P0300", "P0171"]) as emu:
        fd = os.open(emu.port, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(fd)
        try:
            assert talk(fd, "ATZ")[-1] == "ELM327 v2.3"
            assert talk(fd, "ATE0") == ["ATE0", "OK"]          # echo still on for this one
            assert talk(fd, "ATI") == ["ELM327 v2.3"]
            assert talk(fd, "0100")[0] == "SEARCHING..."
            assert talk(fd, "ATDPN") == ["A6"]
            assert talk(fd, "0100")[0].startswith("41 00 ")
            assert talk(fd, "0150") == ["NO DATA"]
            rpm = talk(fd, "010C")[0].split()
            assert rpm[:2] == ["41", "0C"] and len(rpm) == 4
            assert talk(fd, "ATH1") == ["OK"]
            # 4 DTCs = 10 bytes -> ISO-TP multi frame
            frames = talk(fd, "03")
            assert frames[0].startswith("7E8 10 0A 43 04 01 33")
            assert frames[1].startswith("7E8 21 03 00 01 71")
            assert talk(fd, "04") == ["7E8 01 44"]
            assert talk(fd, "03") == ["7E8 02 43 00"]
        finally:
            os.close(fd)
