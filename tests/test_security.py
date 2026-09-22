import base64
import time

import numpy as np
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.security.crypto import Crypto, hash_pin, verify_pin
from app.security.template_protection import TemplateTransform
from app.security.terminal_auth import NonceCache, SignatureError, canonical_string, verify_request
from tests.conftest import noisy, random_unit
from terminal_client.signing import public_key_b64


@pytest.fixture
def crypto():
    return Crypto.from_settings()


def test_encrypt_roundtrip(crypto):
    blob = crypto.encrypt(b"maxfiy", "user:1")
    assert b"maxfiy" not in blob
    assert crypto.decrypt(blob, "user:1") == b"maxfiy"


def test_ciphertext_swap_between_users_fails(crypto):
    """Hujumchi bazada A foydalanuvchining shablonini B ga ko'chirsa — deshifrlash xato beradi."""
    blob = crypto.encrypt(b"shablon", "tpl:userA:t1")
    with pytest.raises(InvalidTag):
        crypto.decrypt(blob, "tpl:userB:t1")


def test_tampered_ciphertext_fails(crypto):
    blob = bytearray(crypto.encrypt(b"shablon", "x"))
    blob[-1] ^= 1
    with pytest.raises(InvalidTag):
        crypto.decrypt(bytes(blob), "x")


def test_phone_lookup_hash_normalized(crypto):
    assert crypto.lookup_hash("+998 90 123-45-67") == crypto.lookup_hash("+998901234567")
    assert crypto.lookup_hash("+998901234567") != crypto.lookup_hash("+998901234568")


def test_pin_hash():
    h = hash_pin("4821")
    assert "4821" not in h
    assert verify_pin("4821", h)
    assert not verify_pin("4822", h)
    assert hash_pin("4821") != h  # har safar yangi tuz


def test_template_transform_preserves_similarity(rng):
    t = TemplateTransform(b"seed-1", 512)
    a = random_unit(rng)
    b = noisy(a, rng, 0.7)
    pa, pb = t.protect(a), t.protect(b)
    assert abs(float(pa @ pb) - float(a @ b)) < 1e-4       # aniqlik yo'qolmaydi
    assert abs(float(pa @ a)) < 0.2                          # saqlangan vektor asl vektorga o'xshamaydi


def test_template_transform_revocable(rng):
    """Urug' almashtirilsa, eski shablonlar yangi tizimda mos kelmaydi (bekor qilish)."""
    a = random_unit(rng)
    old, new = TemplateTransform(b"eski", 512), TemplateTransform(b"yangi", 512)
    assert abs(float(old.protect(a) @ new.protect(a))) < 0.2


def _signed(key, body=b'{"amount": 1700}', ts=None, nonce="a" * 32, path="/v1/payments"):
    ts = str(int(time.time())) if ts is None else ts
    sig = base64.b64encode(key.sign(canonical_string("POST", path, ts, nonce, body))).decode()
    return ts, nonce, body, sig


def test_terminal_signature_and_replay():
    key = Ed25519PrivateKey.generate()
    pub = public_key_b64(key)
    cache = NonceCache(60)
    ts, nonce, body, sig = _signed(key)
    verify_request(pub, "POST", "/v1/payments", ts, nonce, body, sig, "t1", cache, 30)
    with pytest.raises(SignatureError, match="replay"):
        verify_request(pub, "POST", "/v1/payments", ts, nonce, body, sig, "t1", cache, 30)


def test_terminal_signature_tampered_body():
    key = Ed25519PrivateKey.generate()
    ts, nonce, _, sig = _signed(key)
    with pytest.raises(SignatureError, match="Imzo"):
        verify_request(public_key_b64(key), "POST", "/v1/payments", ts, nonce, b'{"amount": 9999999}',
                       sig, "t1", NonceCache(60), 30)


def test_terminal_signature_expired():
    key = Ed25519PrivateKey.generate()
    ts, nonce, body, sig = _signed(key, ts=str(int(time.time()) - 120))
    with pytest.raises(SignatureError, match="muddati"):
        verify_request(public_key_b64(key), "POST", "/v1/payments", ts, nonce, body, sig, "t1", NonceCache(60), 30)


def test_terminal_signature_wrong_key():
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    ts, nonce, body, sig = _signed(other)
    with pytest.raises(SignatureError):
        verify_request(public_key_b64(key), "POST", "/v1/payments", ts, nonce, body, sig, "t1", NonceCache(60), 30)
