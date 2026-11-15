import os
import tempfile

import pytest

# never touch the user's real data directory from the tests
os.environ["OBDRUID_DATA"] = tempfile.mkdtemp(prefix="obdruid-test-")

from obdruid.core import ConnectionSettings, ObdSession


@pytest.fixture(scope="module")
def session():
    s = ObdSession(ConnectionSettings(simulate=True))
    s.connect()
    yield s
    s.close()
