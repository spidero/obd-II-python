import pytest

from obd_tool.core import ConnectionSettings, ObdSession


@pytest.fixture(scope="module")
def session():
    s = ObdSession(ConnectionSettings(simulate=True))
    s.connect()
    yield s
    s.close()
