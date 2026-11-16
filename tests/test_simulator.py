import pytest

from obdruid.core.simulator import Elm327Emulator, default_transport
from simlink import SimLink


def talk(link, cmd, timeout=3.0):
    link.write((cmd + "\r").encode())
    buf = b""
    while b">" not in buf:
        chunk = link.read(timeout=timeout)
        assert chunk, f"timeout waiting for answer to {cmd!r}, got {buf!r}"
        buf += chunk
    return [line for line in buf.decode().replace(">", "").split("\r") if line]


# the pseudo terminal where the system has one, and the TCP port everywhere (it is the only one on Windows)
TRANSPORTS = sorted({default_transport(), "tcp"})


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_raw_dialogue(transport):
    with Elm327Emulator(stored_dtcs=["P0133", "P0420", "P0300", "P0171"], transport=transport) as emu:
        fd = SimLink(emu, raw=True)
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
            fd.close()
