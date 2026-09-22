"""Testlar haqiqiy neyron tarmoqsiz ishlaydi: yuz analizatori soxta (mock) bilan almashtiriladi.

Soxta analizator har bir kadrning (0,0) pikselidan kadr ID sini o'qiydi va oldindan
belgilangan Face obyektini qaytaradi. Shunday qilib, API ning barcha xavfsizlik
mantiqini (imzo, replay, liveness ketma-ketligi, 1:N, PIN, limitlar) sinash mumkin.
"""
import base64
import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.update({
    "FACEPAY_MASTER_KEY_B64": base64.b64encode(b"m" * 32).decode(),
    "FACEPAY_LOOKUP_KEY_B64": base64.b64encode(b"l" * 32).decode(),
    "FACEPAY_TEMPLATE_TRANSFORM_SEED_B64": base64.b64encode(b"s" * 32).decode(),
    "FACEPAY_ADMIN_API_KEY": "test-admin-key",
    "FACEPAY_DATABASE_URL": "sqlite://",
    "FACEPAY_PORTAL_RATE_LIMIT_PER_MINUTE": "1000",
})

DIM = 512


def random_unit(rng: np.random.Generator) -> np.ndarray:
    v = rng.standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def noisy(base: np.ndarray, rng: np.random.Generator, similarity: float = 0.85) -> np.ndarray:
    """base ga taxminan `similarity` cosine o'xshashlikdagi vektor (bir odamning boshqa surati)."""
    n = random_unit(rng)
    n -= (n @ base) * base
    n /= np.linalg.norm(n)
    v = similarity * base + np.sqrt(1 - similarity**2) * n
    return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture
def rng():
    return np.random.default_rng(42)
