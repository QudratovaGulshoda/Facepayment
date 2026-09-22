"""Biometrik shablonni himoyalash (cancelable biometrics).

Muammo: parolni almashtirish mumkin, yuzni esa yo'q. Agar bazadagi embedding'lar
o'g'irlansa, ularni boshqa tizimlarga qarshi ishlatish mumkin bo'lmasligi kerak.

Yechim (3 qatlam):
 1. Embedding maxfiy ortogonal matritsa bilan "aylantiriladi" (random projection).
    Ortogonal almashtirish cosine o'xshashlikni saqlaydi, shuning uchun aniqlik
    yo'qolmaydi, lekin saqlangan vektor asl ArcFace vektoriga o'xshamaydi.
 2. Natija AES-256-GCM bilan shifrlanadi (crypto.py).
 3. Sizib chiqish holatida urug' (seed) almashtiriladi -> barcha eski shablonlar
    yaroqsiz bo'ladi ("bekor qilish"), foydalanuvchilar qayta ro'yxatdan o'tadi.

Rasm (foto) hech qachon saqlanmaydi — faqat himoyalangan vektor.
"""
import base64
import hashlib

import numpy as np

from app.config import get_settings


class TemplateTransform:
    def __init__(self, seed: bytes, dim: int):
        # Urug'dan deterministik ortogonal matritsa (QR dekompozitsiya)
        rng = np.random.default_rng(int.from_bytes(hashlib.sha256(seed).digest(), "big"))
        q, r = np.linalg.qr(rng.standard_normal((dim, dim)))
        q *= np.sign(np.diag(r))  # yagona (unique) ko'rinishga keltirish
        self._matrix = q.astype(np.float32)
        self.dim = dim

    @classmethod
    def from_settings(cls) -> "TemplateTransform":
        s = get_settings()
        if not s.template_transform_seed_b64:
            raise RuntimeError("FACEPAY_TEMPLATE_TRANSFORM_SEED_B64 sozlanmagan")
        return cls(base64.b64decode(s.template_transform_seed_b64), s.embedding_dim)

    def protect(self, embedding: np.ndarray) -> np.ndarray:
        v = normalize(embedding)
        return normalize(self._matrix @ v)

    @staticmethod
    def to_bytes(vec: np.ndarray) -> bytes:
        return vec.astype(np.float32).tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        vec = np.frombuffer(data, dtype=np.float32)
        if vec.shape[0] != self.dim:
            raise ValueError("Shablon o'lchami noto'g'ri")
        return vec.copy()


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = np.linalg.norm(v)
    if n < 1e-8:
        raise ValueError("Bo'sh embedding")
    return v / n
