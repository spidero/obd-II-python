import obd

from obdruid.core import ConnectionSettings, ObdSession
from obdruid.web.server import AppState


def test_watchdog_reconnects_after_a_lost_link(tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    state = AppState(ConnectionSettings(simulate=True))
    state.connect()
    assert state.state == "connected" and state.auto_reconnect
    state.session.close()                      # the adapter disappears
    state.watchdog_step(now=100.0)
    assert state.state == "reconnecting" and state.attempt == 1
    state.watchdog_step(now=101.0)             # too early for attempt 1
    assert state.state == "reconnecting"
    state.watchdog_step(now=106.0)
    assert state.state == "connected" and state.attempt == 0
    assert state.to_dict()["attempt"] == 0
    state.disconnect()


def test_no_reconnect_after_disconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    state = AppState(ConnectionSettings(simulate=True))
    state.connect()
    state.disconnect()
    state.watchdog_step(now=100.0)
    state.watchdog_step(now=200.0)
    assert state.state == "disconnected"


def test_failed_attempts_back_off(tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    state = AppState(ConnectionSettings(simulate=True))
    state.connect()
    state.session.close()

    def broken(settings=None):
        state.state, state.error = "error", "No response from the adapter"
        raise RuntimeError("down")

    monkeypatch.setattr(state, "connect", broken)
    state.watchdog_step(now=0.0)
    state.watchdog_step(now=10.0)              # attempt 1 fails
    assert state.state == "reconnecting" and state.attempt == 2 and state.error


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def is_null(self):
        return self.value is None


class FakeConn:
    """PIDS_B announced PIDS_C (41-60), but the PIDS_C answer was lost the first time."""

    def __init__(self):
        self.supported_commands = {obd.commands.PIDS_A, obd.commands.PIDS_B, obd.commands.PIDS_C,
                                   obd.commands.RPM, obd.commands.DISTANCE_W_MIL}
        self.answers = [None, [i == 0x42 - 0x41 for i in range(32)]]  # 1st try lost; 2nd: 0x42 supported

    def supports(self, cmd):
        return cmd in self.supported_commands

    def query(self, cmd):
        assert cmd is obd.commands.PIDS_C
        return FakeResponse(self.answers.pop(0))


def test_lost_pid_mask_is_asked_again():
    conn = FakeConn()
    ObdSession._complete_pid_masks(conn)
    assert obd.commands.CONTROL_MODULE_VOLTAGE in conn.supported_commands
    assert not conn.answers                    # asked twice: lost, then answered
