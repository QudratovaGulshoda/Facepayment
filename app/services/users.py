"""Ro'yxatdan o'tish, qo'shimcha shablon (makiyaj/ko'zoynak) qo'shish, ma'lumotlarni o'chirish."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.biometrics.pipeline import CaptureResult
from app.config import get_settings
from app.core import audit, get_core
from app.db import FaceTemplate, MatchEvent, Terminal, User, new_id, utcnow
from app.security.crypto import hash_pin, verify_pin


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def mask_name(full_name: str) -> str:
    """Terminal ekranida ko'rsatish uchun: 'Gulnora Karimova' -> 'G****** K.'"""
    parts = full_name.split()
    if not parts:
        return "***"
    first = parts[0][0] + "*" * max(len(parts[0]) - 1, 2)
    return f"{first} {parts[1][0]}." if len(parts) > 1 else first


def _add_template(db: Session, user: User, protected_vec: np.ndarray, source: str, label: str | None = None) -> FaceTemplate:
    core = get_core()
    tid = new_id()
    t = FaceTemplate(id=tid, user_id=user.id, source=source, label=label,
                     template_enc=core.encrypt_template(user.id, tid, protected_vec))
    db.add(t)
    return t


def find_user_by_phone(db: Session, phone: str) -> User | None:
    return db.scalars(select(User).where(User.phone_hash == get_core().crypto.lookup_hash(phone))).first()


def check_not_locked(user: User) -> None:
    if user.status != "active":
        raise ServiceError("foydalanuvchi_faol_emas", 403)
    if user.locked_until and user.locked_until > utcnow():
        raise ServiceError("vaqtincha_bloklangan", 423)


def verify_user_pin(db: Session, user: User, pin: str) -> None:
    s = get_settings()
    check_not_locked(user)
    if verify_pin(pin, user.pin_hash):
        user.pin_failed = 0
        return
    user.pin_failed += 1
    if user.pin_failed >= s.pin_max_attempts:
        user.locked_until = utcnow() + timedelta(minutes=s.lockout_minutes)
        user.pin_failed = 0
        audit(db, "system", "user_locked_pin", user_id=user.id)
    db.commit()
    raise ServiceError("pin_notogri", 401)


def enroll(db: Session, terminal: Terminal, phone: str, full_name: str, pin: str,
           consent: bool, capture: CaptureResult) -> User:
    core = get_core()
    s = get_settings()
    if not consent:
        # "Shaxsga doir ma'lumotlar to'g'risida"gi Qonun: biometrik ma'lumot faqat rozilik bilan
        raise ServiceError("rozilik_berilmagan")
    if find_user_by_phone(db, phone):
        raise ServiceError("telefon_royxatda", 409)

    protected = core.transform.protect(capture.embedding)
    # Bir odam bir nechta hisob ochmasligi (va boshqa birovning yuziga hisob ochilmasligi) uchun
    dup = core.gallery.search(protected)
    if dup.user_id and dup.score >= s.match_threshold:
        audit(db, f"terminal:{terminal.id}", "enroll_duplicate_face", existing_user=dup.user_id)
        db.commit()
        raise ServiceError("yuz_allaqachon_royxatda", 409)

    user = User(id=new_id(), consent_at=utcnow(), pin_hash=hash_pin(pin),
                phone_hash=core.crypto.lookup_hash(phone), phone_enc=b"", full_name_enc=b"")
    user.phone_enc = core.crypto.encrypt_str(phone, f"user:{user.id}:phone")
    user.full_name_enc = core.crypto.encrypt_str(full_name, f"user:{user.id}:name")
    db.add(user)
    db.flush()

    templates = [(_add_template(db, user, protected, "enroll", "asosiy"), protected)]
    for extra in capture.extra_embeddings:
        p = core.transform.protect(extra)
        templates.append((_add_template(db, user, p, "enroll", "burchak"), p))

    audit(db, f"terminal:{terminal.id}", "enroll", user_id=user.id, templates=len(templates),
          quality=round(capture.quality, 3))
    db.commit()
    for t, vec in templates:
        core.gallery.upsert(user.id, t.id, vec)
    return user


def add_variant(db: Session, terminal: Terminal, phone: str, pin: str, label: str,
                capture: CaptureResult) -> FaceTemplate:
    """Masalan: kuchli makiyaj, yangi ko'zoynak, soqol. PIN + yuz bilan tasdiqlanadi."""
    core = get_core()
    s = get_settings()
    user = find_user_by_phone(db, phone)
    if not user:
        raise ServiceError("topilmadi", 404)
    verify_user_pin(db, user, pin)

    protected = core.transform.protect(capture.embedding)
    own = [core.decrypt_template(t) for t in user.templates]
    own_score = max(float(v @ protected) for v in own) if own else 0.0
    other = core.gallery.search(protected, exclude_user=user.id)
    # PIN tasdiqlangani uchun 1:1 chegarasi yumshoqroq, lekin boshqa odamga o'xshab qolmasligi shart
    if own_score < s.match_threshold - 0.12:
        raise ServiceError("yuz_mos_emas", 401)
    if other.user_id and other.score >= own_score - s.min_margin:
        raise ServiceError("boshqa_shaxsga_oxshash", 409)

    if len(user.templates) >= s.max_templates_per_user:
        # Eng uzoq vaqt ishlatilmagan (asosiy bo'lmagan) shablonni almashtiramiz
        removable = [t for t in user.templates if t.label != "asosiy"]
        victim = min(removable, key=lambda t: t.last_matched_at or t.updated_at)
        db.delete(victim)
        core.gallery.remove_template(victim.id)

    t = _add_template(db, user, protected, "variant", label[:32])
    audit(db, f"terminal:{terminal.id}", "template_variant_added", user_id=user.id, template_id=t.id)
    db.commit()
    core.gallery.upsert(user.id, t.id, protected)
    return t


def delete_user_data(db: Session, terminal: Terminal, phone: str, pin: str, capture: CaptureResult) -> None:
    """Unutilish huquqi: biometrik shablonlar, kartalar va shaxsiy ma'lumotlar butunlay o'chiriladi.
    Tranzaksiyalar (buxgalteriya talabi) anonim ID bilan qoladi."""
    core = get_core()
    s = get_settings()
    user = find_user_by_phone(db, phone)
    if not user:
        raise ServiceError("topilmadi", 404)
    verify_user_pin(db, user, pin)
    protected = core.transform.protect(capture.embedding)
    own = [core.decrypt_template(t) for t in user.templates]
    if not own or max(float(v @ protected) for v in own) < s.match_threshold:
        raise ServiceError("yuz_mos_emas", 401)

    from app.services.cards import remove_all_for_user  # aylanma importdan qochish

    remove_all_for_user(db, user.id)
    db.execute(delete(FaceTemplate).where(FaceTemplate.user_id == user.id))
    db.execute(delete(MatchEvent).where(MatchEvent.user_id == user.id))
    user.phone_enc = b""
    user.full_name_enc = b""
    user.phone_hash = f"deleted:{user.id}"
    user.pin_hash = "!"
    user.status = "deleted"
    audit(db, f"terminal:{terminal.id}", "user_data_deleted", user_id=user.id)
    db.commit()
    core.gallery.remove_user(user.id)
