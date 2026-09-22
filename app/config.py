"""Tizim sozlamalari.

Barcha maxfiy kalitlar faqat muhit o'zgaruvchilaridan (yoki .env fayldan) olinadi.
Kodga hech qachon kalit yozilmaydi.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FACEPAY_", extra="ignore")

    # --- Ma'lumotlar bazasi ---
    database_url: str = "sqlite:///./facepay.db"

    # --- Kriptografiya ---
    # 32 baytlik master kalit (base64). Prod'da HSM/KMS dan olinadi.
    master_key_b64: str = ""
    # Master kalit versiyasi: kalit almashtirilganda (rotation) eski yozuvlarni farqlash uchun
    master_key_version: int = 1
    # Telefon raqam kabi ma'lumotlarni qidirish uchun HMAC kaliti (base64)
    lookup_key_b64: str = ""
    # "Bekor qilinadigan biometriya" (cancelable biometrics) uchun maxfiy urug' (base64)
    template_transform_seed_b64: str = ""

    # --- Yuzni solishtirish chegaralari (ArcFace cosine o'xshashligi) ---
    embedding_dim: int = 512
    match_threshold: float = 0.45       # bundan past -> rad etiladi
    high_confidence_threshold: float = 0.60  # shablonni yangilash uchun (yosh o'zgarishi)
    min_margin: float = 0.08            # 1-o'rin va 2-o'rin orasidagi minimal farq (egizaklar, o'xshashlar)
    max_templates_per_user: int = 6     # turli holatlar: makiyajli/makiyajsiz, ko'zoynak, yillar
    template_ema_alpha: float = 0.15    # shablonni asta-sekin yangilash koeffitsienti
    template_diversity_threshold: float = 0.80  # yangi shablon qo'shish uchun farq chegarasi
    reenroll_after_days: int = 3 * 365  # shuncha vaqtdan keyin qayta ro'yxatdan o'tish taklif qilinadi

    # --- Tirik ekanligini tekshirish (liveness) ---
    challenge_ttl_seconds: int = 30
    challenge_steps: int = 2            # tasodifiy harakatlar soni (ko'z qisish, bosh burish ...)
    passive_liveness_threshold: float = 0.70
    min_frames: int = 8

    # --- Sifat talablari ---
    min_face_size_px: int = 112
    min_sharpness: float = 60.0         # Laplacian dispersiyasi
    min_brightness: float = 50.0
    max_brightness: float = 210.0
    max_yaw_deg: float = 25.0
    max_pitch_deg: float = 20.0

    # --- To'lov qoidalari (so'mda) ---
    face_only_limit: int = 200_000      # shu summagacha faqat yuz bilan
    daily_limit: int = 5_000_000
    pin_max_attempts: int = 5
    face_max_failed_attempts: int = 5   # ketma-ket muvaffaqiyatsiz urinishlar -> vaqtincha blok
    lockout_minutes: int = 15

    # --- Terminal autentifikatsiyasi ---
    request_max_skew_seconds: int = 30

    # --- Rate limit ---
    rate_limit_per_minute: int = 120        # terminal: mijoz ekrani har 2 s da so'rovlarni tekshiradi
    portal_rate_limit_per_minute: int = 20   # ochiq mijoz sayti: bitta IP dan

    # --- Karta protsessingi ---
    payment_gateway: str = "mock"       # mock | payme
    payme_url: str = "https://checkout.test.paycom.uz/api"
    payme_merchant_id: str = ""
    payme_key: str = ""
    max_cards_per_user: int = 3
    card_verify_max_attempts: int = 3

    # --- Admin ---
    admin_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
