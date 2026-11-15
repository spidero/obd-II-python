"""Supporter keys: a signed key that hides the "Support the project" window.

A key looks like ``OBDT1-<payload>.<signature>`` (base64url). The payload is a small JSON object
{"n": name, "id": key id, "d": issue date}; the signature is Ed25519 over the payload bytes. Keys are
checked offline against the public key below; only the author holds the private key.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

PREFIX = "OBDT1-"
PUBLIC_KEY = "8X9E7UqLWSLU2CqeRCfxFnYuYfW9hrEfRawDAojKOcE="  # Ed25519, raw, base64
FILE_NAME = "supporter.json"


class InvalidKey(ValueError):
    """The text is not a valid supporter key."""


@dataclass
class SupporterKey:
    name: str
    id: str
    issued: str
    key: str

    def to_dict(self, with_key: bool = False) -> dict:
        data = asdict(self)
        if not with_key:
            data.pop("key")
        return data


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def verify(text: str, public_key: str = PUBLIC_KEY) -> SupporterKey:
    """Check a supporter key and return its contents; raises InvalidKey."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key = re.sub(r"\s+", "", text or "")  # keys are often pasted with line breaks
    if not key.startswith(PREFIX) or key.count(".") != 1:
        raise InvalidKey("This is not an OBD Tool supporter key.")
    payload_b64, signature_b64 = key[len(PREFIX):].split(".")
    try:
        payload, signature = _b64decode(payload_b64), _b64decode(signature_b64)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(signature, payload)
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, InvalidSignature) as e:
        raise InvalidKey("The supporter key is not valid. Check that it was copied completely.") from e
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("n", "id", "d")):
        raise InvalidKey("The supporter key is not valid.")
    return SupporterKey(name=data["n"], id=data["id"], issued=data["d"], key=key)


def sign(name: str, key_id: str, issued: str, private_key_pem: bytes) -> str:
    """Create a key (used by the author's key tool; the private key never ships with the program)."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    payload = json.dumps({"n": name, "id": key_id, "d": issued}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    signature = load_pem_private_key(private_key_pem, password=None).sign(payload)
    return f"{PREFIX}{_b64encode(payload)}.{_b64encode(signature)}"


def load(root: Path) -> Optional[SupporterKey]:
    """The activated key stored in the data folder, or None (a broken or foreign file counts as none)."""
    try:
        data = json.loads((Path(root) / FILE_NAME).read_text("utf-8"))
        return verify(data["key"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(root: Path, text: str) -> SupporterKey:
    """Verify and store a key; raises InvalidKey."""
    supporter = verify(text)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / FILE_NAME).write_text(json.dumps({"key": supporter.key}) + "\n", "utf-8")
    return supporter


def remove(root: Path) -> None:
    (Path(root) / FILE_NAME).unlink(missing_ok=True)
