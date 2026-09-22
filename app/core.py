"""Ilova bo'ylab yagona obyektlar (kripto, galereya, nonce keshi) va audit jurnali."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.biometrics.matcher import Gallery
from app.config import get_settings
from app.db import AuditLog, FaceTemplate, User, utcnow
from app.security.crypto import Crypto
from app.security.template_protection import TemplateTransform
from app.security.terminal_auth import NonceCache, RateLimiter


class Core:
    def __init__(self):
        s = get_settings()
        self.crypto = Crypto.from_settings()
        self.transform = TemplateTransform.from_settings()
        self.gallery = Gallery(s.embedding_dim)
        self.nonces = NonceCache(ttl_seconds=s.request_max_skew_seconds * 2 + 5)
        self.rate_limiter = RateLimiter(s.rate_limit_per_minute)

    # Shablon shifrlanganda AAD = foydalanuvchi + shablon ID si
    @staticmethod
    def template_aad(user_id: str, template_id: str) -> str:
        return f"tpl:{user_id}:{template_id}"

    def encrypt_template(self, user_id: str, template_id: str, protected_vec) -> bytes:
        return self.crypto.encrypt(self.transform.to_bytes(protected_vec), self.template_aad(user_id, template_id))

    def decrypt_template(self, t: FaceTemplate):
        return self.transform.from_bytes(self.crypto.decrypt(t.template_enc, self.template_aad(t.user_id, t.id)))

    def load_gallery(self, db: Session) -> int:
        rows = []
        q = select(FaceTemplate).join(User).where(User.status == "active")
        for t in db.scalars(q):
            rows.append((t.user_id, t.id, self.decrypt_template(t)))
        self.gallery.replace_all(rows)
        return len(rows)


_core: Core | None = None


def get_core() -> Core:
    global _core
    if _core is None:
        _core = Core()
    return _core


def reset_core() -> None:
    global _core
    _core = None


# ---------------- Audit ----------------

def _hash_entry(prev_hash: str, ts: str, actor: str, action: str, details: dict) -> str:
    payload = json.dumps([prev_hash, ts, actor, action, details], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def audit(db: Session, actor: str, action: str, **details) -> None:
    """Jurnalga FAQAT ID lar va hodisa kodlari yoziladi (ism, telefon, biometrik ma'lumot yo'q)."""
    last = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(1)).first()
    prev = last.hash if last else "0" * 64
    ts = utcnow()
    entry = AuditLog(ts=ts, actor=actor, action=action, details=details, prev_hash=prev,
                     hash=_hash_entry(prev, ts.isoformat(), actor, action, details))
    db.add(entry)


def verify_audit_chain(db: Session) -> tuple[bool, int | None]:
    """Jurnal o'zgartirilmaganini tekshiradi. Buzilgan bo'lsa birinchi buzilgan yozuv ID sini qaytaradi."""
    prev = "0" * 64
    for e in db.scalars(select(AuditLog).order_by(AuditLog.id)):
        if e.prev_hash != prev or e.hash != _hash_entry(prev, e.ts.isoformat(), e.actor, e.action, e.details):
            return False, e.id
        prev = e.hash
    return True, None
