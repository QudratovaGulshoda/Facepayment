"""Ma'lumotlar bazasi modellari.

Qoida: bazada ochiq ko'rinishdagi shaxsiy ma'lumot YO'Q.
 - ism, telefon -> AES-GCM bilan shifrlangan (+ telefon uchun HMAC qidiruv xeshi)
 - yuz -> himoyalangan (aylantirilgan) va shifrlangan vektor, rasm saqlanmaydi
 - PIN -> scrypt xesh
 - audit jurnali -> faqat ID lar va hodisa turlari, biometrik ma'lumot yo'q
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    phone_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    phone_enc: Mapped[bytes] = mapped_column(LargeBinary)
    full_name_enc: Mapped[bytes] = mapped_column(LargeBinary)
    pin_hash: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | blocked | deleted
    pin_failed: Mapped[int] = mapped_column(Integer, default=0)
    face_failed: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consent_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_enrolled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    balance: Mapped[int] = mapped_column(Integer, default=0)  # so'm (demo; real tizimda bank hisobi)

    templates: Mapped[list["FaceTemplate"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class FaceTemplate(Base):
    __tablename__ = "face_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    template_enc: Mapped[bytes] = mapped_column(LargeBinary)
    # enroll: ro'yxatdan o'tishda; variant: qo'shimcha holat (makiyaj, ko'zoynak); adaptive: avtomatik yangilangan
    source: Mapped[str] = mapped_column(String(16), default="enroll")
    label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_matched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    match_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="templates")


class Card(Base):
    """Bank kartasi. Karta raqami saqlanmaydi: faqat protsessing tokeni (shifrlangan) va niqob."""

    __tablename__ = "cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    masked: Mapped[str] = mapped_column(String(24))     # 8600 **** **** 1234
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MatchEvent(Base):
    """Moslik ballari tarixi — yosh bilan yuz o'zgarishini (drift) aniqlash uchun."""

    __tablename__ = "match_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    balance: Mapped[int] = mapped_column(Integer, default=0)


class Terminal(Base):
    __tablename__ = "terminals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"), nullable=True)
    public_key_b64: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="payment")  # payment | enroll | cashier | portal
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Challenge(Base):
    __tablename__ = "challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), index=True)
    steps: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("terminal_id", "idempotency_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    # pending_pin | approved | declined
    status: Mapped[str] = mapped_column(String(16))
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)  # face | face+pin
    funding: Mapped[str | None] = mapped_column(String(24), nullable=True)  # balance | 8600 **** **** 1234
    provider_tx_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pin_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PairingCode(Base):
    """Mijoz ekrani ko'rsatadigan bir martalik kod: kassa shu kod bilan ulanadi (10 daqiqa)."""

    __tablename__ = "pairing_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), index=True)
    camera_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class TerminalPairing(Base):
    """Kassa (cashier) qurilmasi qaysi mijoz ekraniga (kamera terminaliga) ulangan."""

    __tablename__ = "terminal_pairings"

    cashier_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), primary_key=True)
    camera_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentRequest(Base):
    """Kassa yaratgan to'lov so'rovi. Summani FAQAT kassa belgilaydi, mijoz ekrani o'zgartira olmaydi."""

    __tablename__ = "payment_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    cashier_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), index=True)
    camera_terminal_id: Mapped[str] = mapped_column(ForeignKey("terminals.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    # waiting -> processing -> done | cancelled | expired
    status: Mapped[str] = mapped_column(String(16), default="waiting")
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    customer: Mapped[str | None] = mapped_column(String(64), nullable=True)  # niqoblangan ism
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AuditLog(Base):
    """O'zgartirib bo'lmaydigan (tamper-evident) jurnal: har yozuv oldingisining xeshini saqlaydi."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


_engine = None
_SessionLocal = None


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    # Neon / Supabase "postgresql://..." beradi — psycopg 3 drayverini aniq ko'rsatamiz
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    kwargs = ({"connect_args": {"check_same_thread": False}} if url.startswith("sqlite")
              else {"pool_pre_ping": True, "pool_recycle": 300})
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    return _engine


def get_session():
    if _SessionLocal is None:
        init_engine()
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def session_factory():
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal
