"""Terminal tomonida so'rovlarni Ed25519 bilan imzolash."""
import base64
import json
import secrets
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.security.terminal_auth import canonical_string


def generate_keypair(path: Path) -> str:
    """Yopiq kalitni faylga yozadi (0600), ochiq kalitni base64 qaytaradi."""
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)
    return public_key_b64(key)


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def load_key(path: Path) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def signed_headers(key: Ed25519PrivateKey, terminal_id: str, method: str, path: str, body: bytes) -> dict:
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    sig = key.sign(canonical_string(method, path, ts, nonce, body))
    return {
        "X-Terminal-Id": terminal_id,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


class TerminalClient:
    def __init__(self, base_url: str, terminal_id: str, key: Ed25519PrivateKey, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.terminal_id = terminal_id
        self.key = key
        self.http = http or httpx.Client(timeout=60)

    def post(self, path: str, payload: dict) -> httpx.Response:
        body = json.dumps(payload).encode()
        headers = signed_headers(self.key, self.terminal_id, "POST", path, body)
        return self.http.post(self.base_url + path, content=body, headers=headers)
