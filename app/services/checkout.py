"""Ikki qurilmali kassa: sotuvchi kassasi (cashier) + mijozga qaragan ekran (kamera terminali).

    Mijoz ekrani  --(ulash kodi)-->  Kassa ulanadi
    Kassa: summa -> PaymentRequest(waiting)
    Mijoz ekrani: so'rovni oladi -> mijoz summani ko'rib tasdiqlaydi -> yuz -> to'lov
    Kassa: holatni kuzatadi (to'landi / rad / PIN kutilmoqda)

Xavfsizlik:
  - summa serverda so'rovga yoziladi; mijoz ekrani faqat so'rov ID sini yuboradi
  - ulash kodi: 6 raqam, bir martalik, 10 daqiqa, bazada faqat HMAC xeshi
  - PIN mijoz ekranida kiritiladi — sotuvchi ko'rmaydi
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.biometrics.pipeline import CaptureResult
from app.core import audit, get_core
from app.db import (Merchant, PairingCode, PaymentRequest, Terminal, TerminalPairing, Transaction, new_id,
                    utcnow)
from app.services import payments
from app.services.users import ServiceError

PAIRING_TTL = timedelta(minutes=10)
REQUEST_TTL = timedelta(minutes=3)


def _code_hash(code: str) -> str:
    return get_core().crypto.lookup_hash("pair:" + code)


# ---------------- Ulash ----------------

def new_pairing_code(db: Session, camera: Terminal) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    db.execute(update(PairingCode).where(PairingCode.camera_terminal_id == camera.id, PairingCode.used.is_(False))
               .values(used=True))  # eski kodlar bekor
    db.add(PairingCode(code_hash=_code_hash(code), camera_terminal_id=camera.id, expires_at=utcnow() + PAIRING_TTL))
    audit(db, f"terminal:{camera.id}", "pairing_code_issued")
    db.commit()
    return code


def pair_cashier(db: Session, code: str, public_key_b64: str, name: str) -> tuple[Terminal, Merchant]:
    pc = db.scalars(select(PairingCode).where(PairingCode.code_hash == _code_hash(code),
                                              PairingCode.used.is_(False))).first()
    if not pc or pc.expires_at < utcnow():
        raise ServiceError("ulash_kodi_notogri", 400)
    pc.used = True
    camera = db.get(Terminal, pc.camera_terminal_id)
    cashier = Terminal(id=new_id(), name=name[:120], merchant_id=camera.merchant_id,
                       public_key_b64=public_key_b64, role="cashier")
    db.add(cashier)
    db.flush()
    db.add(TerminalPairing(cashier_terminal_id=cashier.id, camera_terminal_id=camera.id))
    audit(db, f"terminal:{camera.id}", "cashier_paired", cashier_id=cashier.id)
    db.commit()
    return cashier, db.get(Merchant, camera.merchant_id)


def _camera_of(db: Session, cashier: Terminal) -> Terminal:
    p = db.get(TerminalPairing, cashier.id)
    camera = db.get(Terminal, p.camera_terminal_id) if p else None
    if not camera or not camera.active:
        raise ServiceError("mijoz_ekrani_ulanmagan", 409)
    return camera


# ---------------- Kassa tomoni ----------------

def create_request(db: Session, cashier: Terminal, amount: int) -> PaymentRequest:
    if amount <= 0:
        raise ServiceError("summa_notogri")
    camera = _camera_of(db, cashier)
    # Mijoz ekranida bir vaqtda faqat bitta so'rov: eskilari bekor qilinadi
    db.execute(update(PaymentRequest).where(PaymentRequest.camera_terminal_id == camera.id,
                                            PaymentRequest.status == "waiting").values(status="cancelled"))
    req = PaymentRequest(id=new_id(), cashier_terminal_id=cashier.id, camera_terminal_id=camera.id,
                         amount=amount, expires_at=utcnow() + REQUEST_TTL)
    db.add(req)
    audit(db, f"terminal:{cashier.id}", "payment_requested", request_id=req.id, amount=amount)
    db.commit()
    return req


def _own_request(db: Session, request_id: str, cashier: Terminal) -> PaymentRequest:
    req = db.get(PaymentRequest, request_id)
    if not req or req.cashier_terminal_id != cashier.id:
        raise ServiceError("sorov_topilmadi", 404)
    return req


def request_status(db: Session, request_id: str, cashier: Terminal) -> dict:
    req = _own_request(db, request_id, cashier)
    if req.status == "waiting" and req.expires_at < utcnow():
        req.status = "expired"
        db.commit()
    out = {"request_id": req.id, "amount": req.amount, "status": req.status}
    if req.transaction_id:
        tx = db.get(Transaction, req.transaction_id)
        out.update(status=tx.status, method=tx.method, funding=tx.funding, reason=tx.decline_reason)
        if req.customer:
            out["customer"] = req.customer
    return out


def cancel_request(db: Session, request_id: str, cashier: Terminal) -> None:
    req = _own_request(db, request_id, cashier)
    if req.status == "waiting":
        req.status = "cancelled"
        audit(db, f"terminal:{cashier.id}", "payment_request_cancelled", request_id=req.id)
        db.commit()


def cashier_history(db: Session, cashier: Terminal) -> dict:
    return payments.terminal_history(db, _camera_of(db, cashier))


# ---------------- Mijoz ekrani tomoni ----------------

def pending_for_camera(db: Session, camera: Terminal) -> dict | None:
    req = db.scalars(select(PaymentRequest).where(
        PaymentRequest.camera_terminal_id == camera.id, PaymentRequest.status == "waiting",
        PaymentRequest.expires_at > utcnow()).order_by(PaymentRequest.created_at.desc())).first()
    if not req:
        return None
    merchant = db.get(Merchant, camera.merchant_id)
    return {"request_id": req.id, "amount": req.amount, "merchant": merchant.name if merchant else ""}


def claim_request(db: Session, camera: Terminal, request_id: str) -> PaymentRequest:
    """Mijoz "To'lash" ni bosdi: so'rov band qilinadi (ikki marta ishlatib bo'lmaydi)."""
    req = db.get(PaymentRequest, request_id)
    if not req or req.camera_terminal_id != camera.id:
        raise ServiceError("sorov_topilmadi", 404)
    if req.status != "waiting" or req.expires_at < utcnow():
        raise ServiceError("sorov_yaroqsiz", 409)
    req.status = "processing"
    db.commit()
    return req


def pay_request(db: Session, camera: Terminal, req: PaymentRequest, capture: CaptureResult) -> tuple[Transaction, dict]:
    # Summa so'rovdan olinadi; idempotentlik kaliti = so'rov ID
    tx, info = payments.create_payment(db, camera, req.id.replace("-", ""), req.amount, capture)
    req.transaction_id = tx.id
    req.status = "done"
    req.customer = info.get("customer")
    db.commit()
    return tx, info


def release_request(db: Session, req: PaymentRequest) -> None:
    """Biometrik tekshiruv o'tmasa, mijoz qayta urinishi uchun so'rov yana kutish holatiga qaytadi."""
    if req.status == "processing" and not req.transaction_id:
        req.status = "waiting"
        db.commit()
