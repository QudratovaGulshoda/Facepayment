"""To'lov terminallarini autentifikatsiya qilish.

Har bir terminal (metro turniketi, do'kon kassasi) o'zining Ed25519 kalit juftligiga ega.
Server faqat OCHIQ kalitni saqlaydi — server bazasi o'g'irlansa ham terminal nomidan
so'rov yuborib bo'lmaydi.

Imzolanadigan satr:
    METHOD \n PATH \n TIMESTAMP \n NONCE \n SHA256(body)

Himoya:
 - timestamp: +-30 soniyadan eski so'rovlar rad etiladi
 - nonce: bir marta ishlatiladi (replay hujumidan himoya)
 - body xeshi: so'rov mazmunini (summa, qabul qiluvchi) o'zgartirib bo'lmaydi
"""
import base64
import hashlib
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash]).encode()


class NonceCache:
    """Ishlatilgan nonce'lar. Prod'da Redis (SET NX EX) ishlatiladi."""

    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            if len(self._seen) > 10_000:
                self._seen = {k: t for k, t in self._seen.items() if now - t < self._ttl}
            if key in self._seen and now - self._seen[key] < self._ttl:
                return False
            self._seen[key] = now
            return True


class SignatureError(Exception):
    pass


def verify_request(
    public_key_b64: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature_b64: str,
    terminal_id: str,
    nonce_cache: NonceCache,
    max_skew: int,
) -> None:
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("timestamp noto'g'ri")
    if abs(time.time() - ts) > max_skew:
        raise SignatureError("So'rov muddati o'tgan (soat farqi)")
    if not (16 <= len(nonce) <= 64):
        raise SignatureError("nonce noto'g'ri")

    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        pub.verify(base64.b64decode(signature_b64), canonical_string(method, path, timestamp, nonce, body))
    except (InvalidSignature, ValueError):
        raise SignatureError("Imzo noto'g'ri")

    # Imzo to'g'ri bo'lgandan keyingina nonce saqlanadi (aks holda hujumchi keshni to'ldirishi mumkin)
    if not nonce_cache.check_and_add(f"{terminal_id}:{nonce}"):
        raise SignatureError("Takroriy so'rov (replay)")


class RateLimiter:
    """Oddiy sirpanuvchi oyna. Prod'da Redis."""

    def __init__(self, per_minute: int):
        self._limit = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < 60]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True
