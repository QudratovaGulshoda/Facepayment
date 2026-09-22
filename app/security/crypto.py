"""Kriptografik yordamchilar.

- AES-256-GCM: biometrik shablon va shaxsiy ma'lumotlarni shifrlash.
  AAD (qo'shimcha autentifikatsiyalangan ma'lumot) sifatida yozuv egasining ID si
  ishlatiladi: shifrlangan shablonni boshqa foydalanuvchiga ko'chirib qo'yish
  (ciphertext swap) hujumi ishlamaydi.
- HMAC-SHA256: telefon raqam kabi maydonlarni ochiq saqlamasdan qidirish.
- scrypt: PIN kodni xeshlash (sekin, tuzli).
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_NONCE_LEN = 12


def _b64key(value: str, name: str) -> bytes:
    if not value:
        raise RuntimeError(f"{name} sozlanmagan. scripts/generate_keys.py ni ishga tushiring.")
    key = base64.b64decode(value)
    if len(key) != 32:
        raise RuntimeError(f"{name} 32 bayt bo'lishi kerak")
    return key


class Crypto:
    def __init__(self, master_key: bytes, key_version: int, lookup_key: bytes):
        self._aes = AESGCM(master_key)
        self._version = key_version
        self._lookup_key = lookup_key

    @classmethod
    def from_settings(cls) -> "Crypto":
        s = get_settings()
        return cls(
            _b64key(s.master_key_b64, "FACEPAY_MASTER_KEY_B64"),
            s.master_key_version,
            _b64key(s.lookup_key_b64, "FACEPAY_LOOKUP_KEY_B64"),
        )

    # Format: [versiya 2 bayt][nonce 12 bayt][ciphertext+tag]
    def encrypt(self, plaintext: bytes, aad: str) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        ct = self._aes.encrypt(nonce, plaintext, aad.encode())
        return struct.pack(">H", self._version) + nonce + ct

    def decrypt(self, blob: bytes, aad: str) -> bytes:
        version = struct.unpack(">H", blob[:2])[0]
        if version != self._version:
            # Real tizimda: versiyaga mos eski kalit KMS dan olinadi va qayta shifrlanadi
            raise ValueError(f"Kalit versiyasi mos emas: {version}")
        nonce, ct = blob[2 : 2 + _NONCE_LEN], blob[2 + _NONCE_LEN :]
        return self._aes.decrypt(nonce, ct, aad.encode())

    def encrypt_str(self, value: str, aad: str) -> bytes:
        return self.encrypt(value.encode(), aad)

    def decrypt_str(self, blob: bytes, aad: str) -> str:
        return self.decrypt(blob, aad).decode()

    def lookup_hash(self, value: str) -> str:
        """Deterministik HMAC: bazada ochiq ko'rinishda saqlamasdan qidirish uchun."""
        normalized = "".join(ch for ch in value if ch.isdigit() or ch == "+")
        return hmac.new(self._lookup_key, normalized.encode(), hashlib.sha256).hexdigest()


# --- PIN ---
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_pin(pin: str) -> str:
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        raise ValueError("PIN 4-6 raqamdan iborat bo'lishi kerak")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pin.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    salt, expected = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
    dk = hashlib.scrypt(pin.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return hmac.compare_digest(dk, expected)
