import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from obdruid.cli import app as cli_app
from obdruid.core import ConnectionSettings, supporter
from obdruid.web.server import create_app


@pytest.fixture
def keypair(monkeypatch):
    """A throw-away key pair; the program is patched to trust its public key."""
    import base64
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(supporter, "PUBLIC_KEY", base64.b64encode(raw).decode())
    monkeypatch.setattr(supporter.verify, "__defaults__", (base64.b64encode(raw).decode(),))
    return pem


def test_sign_and_verify(keypair):
    key = supporter.sign("Jan K.", "ab12cd34", "2026-11-15", keypair)
    s = supporter.verify(key)
    assert (s.name, s.id, s.issued) == ("Jan K.", "ab12cd34", "2026-11-15")
    wrapped = key[:40] + "\n  " + key[40:]  # pasted from an e-mail with a line break
    assert supporter.verify(wrapped).name == "Jan K."


@pytest.mark.parametrize("bad", ["", "hello", "OBDT1-abc", "OBDT1-abc.def"])
def test_invalid_keys(keypair, bad):
    with pytest.raises(supporter.InvalidKey):
        supporter.verify(bad)


def test_tampered_and_foreign_keys(keypair):
    key = supporter.sign("Jan K.", "ab12cd34", "2026-11-15", keypair)
    payload, sig = key[len(supporter.PREFIX):].split(".")
    forged = supporter.PREFIX + supporter._b64encode(b'{"n":"Mallory","id":"x","d":"2026-01-01"}') + "." + sig
    with pytest.raises(supporter.InvalidKey):
        supporter.verify(forged)
    other = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption())
    with pytest.raises(supporter.InvalidKey):
        supporter.verify(supporter.sign("Jan K.", "1", "2026-11-15", other))


def test_storage(keypair, tmp_path):
    assert supporter.load(tmp_path) is None
    supporter.save(tmp_path, supporter.sign("Jan K.", "1", "2026-11-15", keypair))
    assert supporter.load(tmp_path).name == "Jan K."
    (tmp_path / supporter.FILE_NAME).write_text("garbage")
    assert supporter.load(tmp_path) is None
    supporter.remove(tmp_path)
    supporter.remove(tmp_path)  # removing twice is fine


def test_web_api(keypair, tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    with TestClient(create_app(ConnectionSettings(simulate=True), connect=False)) as c:
        assert c.get("/api/supporter").json() == {"active": False}
        assert c.get("/api/about").json()["supporter"]["active"] is False
        r = c.post("/api/supporter", json={"key": "nope"})
        assert r.status_code == 400 and "not an OBDruid supporter key" in r.json()["detail"]
        key = supporter.sign("Jan K.", "1", "2026-11-15", keypair)
        assert c.post("/api/supporter", json={"key": key}).json()["name"] == "Jan K."
        assert "key" not in c.get("/api/supporter").json()  # the key itself is not handed out
        assert c.get("/api/about").json()["supporter"] == {"active": True, "name": "Jan K.", "id": "1",
                                                           "issued": "2026-11-15"}
        assert c.delete("/api/supporter").json() == {"active": False}


def test_cli(keypair, tmp_path, monkeypatch):
    monkeypatch.setenv("OBDRUID_DATA", str(tmp_path))
    run = CliRunner().invoke
    assert "No supporter key" in run(cli_app, ["supporter"]).output
    assert run(cli_app, ["supporter", "OBDT1-x.y"]).exit_code == 1
    r = run(cli_app, ["supporter", supporter.sign("Jan K.", "1", "2026-11-15", keypair)])
    assert r.exit_code == 0 and "Licensed to: Jan K." in r.output
    assert "Licensed to: Jan K." in run(cli_app, ["supporter"]).output
    run(cli_app, ["supporter", "--remove"])
    assert "No supporter key" in run(cli_app, ["supporter"]).output


def test_real_public_key_is_valid():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(supporter.PUBLIC_KEY))
