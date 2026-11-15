from datetime import date

from fastapi.testclient import TestClient

from obdruid.core import ConnectionSettings, usage
from obdruid.web.server import create_app


def test_counters(tmp_path):
    assert usage.summary(tmp_path, date(2026, 11, 15)) == {"starts": 0, "since": "2026-11-15", "days": 1}
    usage.record_start(tmp_path, date(2026, 11, 15))
    u = usage.record_start(tmp_path, date(2026, 11, 24))
    assert u == {"starts": 2, "since": "2026-11-15", "days": 10}
    (tmp_path / usage.FILE_NAME).write_text("garbage")       # a broken file starts over, never fails
    assert usage.record_start(tmp_path, date(2026, 12, 1))["starts"] == 1


def test_each_server_start_is_counted(tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    for expected in (1, 2):
        with TestClient(create_app(ConnectionSettings(simulate=True), connect=False)) as c:
            u = c.get("/api/about").json()["usage"]
            assert u["starts"] == expected and u["days"] == 1
