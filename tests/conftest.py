import os
import tempfile

import pytest

# never touch the user's real data directory from the tests
os.environ["OBD_TOOL_DATA"] = tempfile.mkdtemp(prefix="obd-tool-test-")

from obd_tool.core import ConnectionSettings, ObdSession


@pytest.fixture(scope="module")
def session():
    s = ObdSession(ConnectionSettings(simulate=True))
    s.connect()
    yield s
    s.close()
