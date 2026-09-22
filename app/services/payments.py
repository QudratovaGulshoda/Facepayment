"""Yuz orqali to'lov.

Risk qoidalari:
  - summa <= face_only_limit va ishonchli moslik  -> faqat yuz (metro, kichik xaridlar)
  - summa > face_only_limit                        -> yuz + PIN
  - chegaradagi ball / qayta ro'yxat kerak bo'lsa  -> yuz + PIN
  - kunlik limit, balans, bloklash, idempotentlik
  - pul asosiy bank kartasidan (Uzcard/Humo) yechiladi, karta bo'lmasa — FacePay balansidan
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.biometrics.matcher import TemplateInfo, decide, needs_reenrollment, plan_template_update
from app.biometrics.pipeline import CaptureResult
from app.config import get_settings
from app.core import audit, get_core
from app.db import Card, FaceTemplate, MatchEvent, Merchant, Terminal, Transaction, User, new_id, utcnow
from app.gateway import GatewayError, get_gateway
from app.services.cards import card_token
from app.services.users import ServiceError, check_not_locked, mask_name, verify_user_pin

PIN_WINDOW_SECONDS = 60
MAX_PIN_ATTEMPTS_PER_TX = 3


def _spent_today(db: Session, user_id: str) -> int:
    start = datetime.combine(utcnow().date(), time.min)
    q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.user_id == user_id, Transaction.status == "approved", Transaction.created_at >= start)
    return int(db.scalar(q))


def _decline(db: Session, tx: Transaction, reason: str) -> Transaction:
    tx.status = "declined"
    tx.decline_reason = reason
    audit(db, f"terminal:{tx.terminal_id}", "payment_declined", tx_id=tx.id, reason=reason)
    db.commit()
    return tx


def _approve(db: Session, tx: Transaction, user: User, method: str) -> Transaction:
    """Pulni yechish: asosiy (tasdiqlangan) karta bo'lsa — kartadan, bo'lmasa — FacePay balansidan."""
    if _spent_today(db, user.id) + tx.amount > get_settings().daily_limit:
        return _decline(db, tx, "kunlik_limit")
    card = db.scalars(select(Card).where(Card.user_id == user.id, Card.is_default, Card.verified)).first()

    if card:
        # Protsessingga murojaatdan OLDIN holatni saqlaymiz: server shu yerda yiqilsa,
        # "charging" holatidagi tranzaksiyalar keyin protsessing bilan solishtiriladi (reconciliation)
        tx.status = "charging"
        tx.funding = card.masked
        db.commit()
        try:
            tx.provider_tx_id = get_gateway().charge(card_token(card), tx.amount, tx.id)
        except GatewayError as e:
            return _decline(db, tx, e.code)
        merchant = db.scalars(select(Merchant).where(Merchant.id == tx.merchant_id).with_for_update()).one()
    else:
        # Qayta o'qish + qulflash (PostgreSQL da SELECT ... FOR UPDATE) — bir vaqtdagi ikki to'lovdan himoya
        user = db.scalars(select(User).where(User.id == user.id).with_for_update()).one()
        merchant = db.scalars(select(Merchant).where(Merchant.id == tx.merchant_id).with_for_update()).one()
        if user.balance < tx.amount:
            return _decline(db, tx, "mablag_yetarli_emas")
        user.balance -= tx.amount
        tx.funding = "balance"

    merchant.balance += tx.amount
    tx.status = "approved"
    tx.method = method
    audit(db, f"terminal:{tx.terminal_id}", "payment_approved", tx_id=tx.id, user_id=user.id,
          amount=tx.amount, method=method, funding="card" if card else "balance")
    db.commit()
    return tx


def _adapt_templates(db: Session, user: User, capture: CaptureResult, score: float, matched_tid: str | None) -> None:
    """Yosh o'zgarishi/yangi ko'rinishga moslashish (faqat ishonchli holatlarda)."""
    core = get_core()
    now = utcnow()
    db.add(MatchEvent(user_id=user.id, score=score))
    if matched_tid:
        t = db.get(FaceTemplate, matched_tid)
        if t:
            t.last_matched_at = now
            t.match_count += 1

    if capture.passive_score < 0.8 or capture.quality < 0.6:
        db.commit()
        return
    infos = [TemplateInfo(t.id, core.decrypt_template(t), t.source, t.last_matched_at, t.updated_at)
             for t in user.templates]
    protected = core.transform.protect(capture.embedding)
    plan = plan_template_update(infos, protected, score, now)
    if plan.action in ("ema", "replace"):
        t = db.get(FaceTemplate, plan.template_id)
        t.template_enc = core.encrypt_template(user.id, t.id, plan.new_vec)
        t.updated_at = now
        if plan.action == "replace":
            t.source, t.label, t.created_at = "adaptive", "moslashgan", now
        db.commit()
        core.gallery.upsert(user.id, t.id, plan.new_vec)
    elif plan.action == "add":
        tid = new_id()
        db.add(FaceTemplate(id=tid, user_id=user.id, source="adaptive", label="moslashgan",
                            template_enc=core.encrypt_template(user.id, tid, plan.new_vec)))
        db.commit()
        core.gallery.upsert(user.id, tid, plan.new_vec)
    else:
        db.commit()
    if plan.action != "none":
        audit(db, "system", "template_adapted", user_id=user.id, action=plan.action)
        db.commit()


def create_payment(db: Session, terminal: Terminal, idempotency_key: str, amount: int,
                   capture: CaptureResult) -> tuple[Transaction, dict]:
    s = get_settings()
    core = get_core()

    existing = db.scalars(select(Transaction).where(
        Transaction.terminal_id == terminal.id, Transaction.idempotency_key == idempotency_key)).first()
    if existing:
        return existing, {}
    if amount <= 0:
        raise ServiceError("summa_notogri")
    if not terminal.merchant_id:
        raise ServiceError("terminal_sotuvchiga_boglanmagan")

    tx = Transaction(id=new_id(), terminal_id=terminal.id, idempotency_key=idempotency_key,
                     merchant_id=terminal.merchant_id, amount=amount, status="processing")
    db.add(tx)
    db.flush()

    result = core.gallery.search(core.transform.protect(capture.embedding))
    decision = decide(result)
    tx.score = round(result.score, 4)
    if not decision.accepted:
        return _decline(db, tx, decision.reason), {}

    user = db.get(User, result.user_id)
    tx.user_id = user.id
    try:
        check_not_locked(user)
    except ServiceError as e:
        return _decline(db, tx, e.code), {}

    now = utcnow()
    recent = list(db.scalars(select(MatchEvent.score).where(MatchEvent.user_id == user.id)
                             .order_by(MatchEvent.id.desc()).limit(20)))
    reenroll = needs_reenrollment(recent, user.last_enrolled_at, now)
    name = mask_name(core.crypto.decrypt_str(user.full_name_enc, f"user:{user.id}:name"))
    info = {"customer": name, "reenroll_recommended": reenroll}

    if amount > s.face_only_limit or decision.borderline or reenroll:
        tx.status = "pending_pin"
        tx.expires_at = now + timedelta(seconds=PIN_WINDOW_SECONDS)
        audit(db, f"terminal:{terminal.id}", "payment_pin_required", tx_id=tx.id,
              borderline=decision.borderline, over_limit=amount > s.face_only_limit)
        db.commit()
        return tx, info

    tx = _approve(db, tx, user, "face")
    if tx.status == "approved" and not decision.borderline:
        _adapt_templates(db, user, capture, result.score, result.template_id)
    return tx, info


def confirm_pin(db: Session, terminal: Terminal, tx_id: str, pin: str) -> Transaction:
    tx = db.get(Transaction, tx_id)
    if not tx or tx.terminal_id != terminal.id:
        raise ServiceError("tranzaksiya_topilmadi", 404)
    if tx.status != "pending_pin":
        raise ServiceError("tranzaksiya_holati_notogri", 409)
    if tx.expires_at and tx.expires_at < utcnow():
        return _decline(db, tx, "pin_vaqti_tugadi")

    user = db.get(User, tx.user_id)
    tx.pin_attempts += 1
    try:
        verify_user_pin(db, user, pin)
    except ServiceError as e:
        if tx.pin_attempts >= MAX_PIN_ATTEMPTS_PER_TX or e.code == "vaqtincha_bloklangan":
            return _decline(db, tx, "pin_urinishlar_tugadi")
        db.commit()
        raise
    return _approve(db, tx, user, "face+pin")
