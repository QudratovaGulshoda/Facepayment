"""Bank kartasini ulash, tasdiqlash, ro'yxatini ko'rish va o'chirish.

Himoya:
  - PIN (FacePay PIN'i) — faqat hisob egasi karta qo'sha oladi
  - SMS kod — kartaga bog'langan telefonga keladi, ya'ni karta haqiqatan shu odamniki
  - 3 marta noto'g'ri SMS kod -> karta o'chiriladi, qaytadan ulash kerak
  - karta raqami bazaga ham, jurnalga ham yozilmaydi
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import audit, get_core
from app.db import Card, Terminal, User, new_id
from app.gateway import GatewayError, get_gateway
from app.services.users import ServiceError, find_user_by_phone, verify_user_pin


def _aad(user_id: str, card_id: str) -> str:
    return f"card:{user_id}:{card_id}"


def card_token(card: Card) -> str:
    return get_core().crypto.decrypt_str(card.token_enc, _aad(card.user_id, card.id))


def _user(db: Session, phone: str) -> User:
    user = find_user_by_phone(db, phone)
    if not user:
        raise ServiceError("topilmadi", 404)
    return user


def mask_phone(phone: str) -> str:
    """+998948431011 -> +99894*****11"""
    return phone[:6] + "*" * max(len(phone) - 8, 0) + phone[-2:]


def add_card(db: Session, terminal: Terminal, phone: str, pin: str, number: str, expire: str) -> tuple[Card, str]:
    s = get_settings()
    user = _user(db, phone)
    verify_user_pin(db, user, pin)
    count = db.scalar(select(func.count()).select_from(Card).where(Card.user_id == user.id))
    if count >= s.max_cards_per_user:
        raise ServiceError("kartalar_soni_limit", 409)

    gw = get_gateway()
    try:
        info = gw.create_card(number, expire)
        # Real protsessing kartaga bankda bog'langan raqamni qaytaradi; demo'da — foydalanuvchi raqami
        phone_hint = gw.send_code(info.token) or info.phone_hint or mask_phone(phone)
    except GatewayError as e:
        raise ServiceError(e.code, 400)

    card = Card(id=new_id(), user_id=user.id, provider=gw.name, masked=info.masked, token_enc=b"")
    card.token_enc = get_core().crypto.encrypt_str(info.token, _aad(user.id, card.id))
    db.add(card)
    audit(db, f"terminal:{terminal.id}", "card_added", user_id=user.id, card_id=card.id)
    db.commit()
    return card, phone_hint


def verify_card(db: Session, terminal: Terminal, card_id: str, phone: str, code: str) -> Card:
    s = get_settings()
    user = _user(db, phone)
    card = db.get(Card, card_id)
    if not card or card.user_id != user.id:
        raise ServiceError("karta_topilmadi", 404)
    if card.verified:
        return card
    try:
        get_gateway().verify(card_token(card), code)
    except GatewayError as e:
        card.verify_attempts += 1
        if card.verify_attempts >= s.card_verify_max_attempts:
            db.delete(card)
            audit(db, f"terminal:{terminal.id}", "card_verify_failed_removed", user_id=user.id, card_id=card_id)
            db.commit()
            raise ServiceError("sms_urinishlar_tugadi", 401)
        db.commit()
        raise ServiceError(e.code, 401)

    card.verified = True
    has_default = db.scalar(select(func.count()).select_from(Card).where(
        Card.user_id == user.id, Card.is_default, Card.verified))
    if not has_default:
        card.is_default = True
    audit(db, f"terminal:{terminal.id}", "card_verified", user_id=user.id, card_id=card.id)
    db.commit()
    return card


def list_cards(db: Session, phone: str, pin: str) -> list[Card]:
    user = _user(db, phone)
    verify_user_pin(db, user, pin)
    db.commit()
    return list(db.scalars(select(Card).where(Card.user_id == user.id).order_by(Card.created_at)))


def set_default(db: Session, terminal: Terminal, card_id: str, phone: str, pin: str) -> Card:
    user = _user(db, phone)
    verify_user_pin(db, user, pin)
    card = db.get(Card, card_id)
    if not card or card.user_id != user.id or not card.verified:
        raise ServiceError("karta_topilmadi", 404)
    for c in db.scalars(select(Card).where(Card.user_id == user.id)):
        c.is_default = c.id == card.id
    audit(db, f"terminal:{terminal.id}", "card_default_changed", user_id=user.id, card_id=card.id)
    db.commit()
    return card


def remove_card(db: Session, terminal: Terminal, card_id: str, phone: str, pin: str) -> None:
    user = _user(db, phone)
    verify_user_pin(db, user, pin)
    card = db.get(Card, card_id)
    if not card or card.user_id != user.id:
        raise ServiceError("karta_topilmadi", 404)
    remove_all_for_user(db, user.id, only=card)
    audit(db, f"terminal:{terminal.id}", "card_removed", user_id=user.id, card_id=card_id)
    db.commit()


def remove_all_for_user(db: Session, user_id: str, only: Card | None = None) -> None:
    """Protsessingdagi tokenni ham o'chiradi (unutilish huquqi)."""
    cards = [only] if only else list(db.scalars(select(Card).where(Card.user_id == user_id)))
    for card in cards:
        try:
            get_gateway().remove(card_token(card))
        except GatewayError:
            pass  # protsessingda allaqachon yo'q — bazadan baribir o'chiramiz
        was_default = card.is_default
        db.delete(card)
        if was_default and only:
            db.flush()
            nxt = db.scalars(select(Card).where(Card.user_id == user_id, Card.verified)).first()
            if nxt:
                nxt.is_default = True
