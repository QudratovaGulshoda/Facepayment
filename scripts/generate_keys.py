"""Server kalitlarini yaratib .env fayliga yozadi (faqat birinchi marta).

    python scripts/generate_keys.py
"""
import base64
import os
import secrets
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"

if ENV.exists() and "--force" not in sys.argv:
    sys.exit(".env allaqachon mavjud. Kalitlarni almashtirish barcha shablonlarni yaroqsiz qiladi! (--force)")


def k() -> str:
    return base64.b64encode(os.urandom(32)).decode()


ENV.write_text(
    f"FACEPAY_MASTER_KEY_B64={k()}\n"
    f"FACEPAY_MASTER_KEY_VERSION=1\n"
    f"FACEPAY_LOOKUP_KEY_B64={k()}\n"
    f"FACEPAY_TEMPLATE_TRANSFORM_SEED_B64={k()}\n"
    f"FACEPAY_ADMIN_API_KEY={secrets.token_urlsafe(32)}\n"
    f"FACEPAY_DATABASE_URL=sqlite:///./facepay.db\n"
)
os.chmod(ENV, 0o600)  # faqat egasi o'qiy oladi
print(f"Kalitlar yozildi: {ENV}")
