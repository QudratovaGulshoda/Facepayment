"""FacePay API.

Ishga tushirish:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Prod'da faqat TLS 1.3 (nginx/traefik orqali) va serverlar O'zbekiston hududida
(Qonun: fuqarolarning shaxsiy ma'lumotlari O'zbekistondagi serverlarda saqlanadi).
"""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.biometrics.analyzer import get_analyzer
from app.biometrics.liveness import ACTION_TEXT_UZ, generate_challenge
from app.biometrics.pipeline import BiometricError, CaptureResult, process_capture
from app.config import get_settings
from app.core import audit, get_core, verify_audit_chain
from app.db import Challenge, Merchant, Terminal, get_session, session_factory, utcnow
from app.schemas import (CaptureIn, CardAddIn, CardVerifyIn, DeleteIn, EnrollIn, MerchantIn, PaymentIn,
                         PhonePinIn, PinIn, PinResetIn, TerminalIn, TopUpIn, VariantIn)
from app.security.terminal_auth import SignatureError, verify_request
from app.services import cards, payments, users
from app.services.users import ServiceError

log = logging.getLogger("facepay")
MAX_BODY_BYTES = 25 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db = session_factory()()
    try:
        n = get_core().load_gallery(db)
        log.info("Galereyaga %d ta shablon yuklandi", n)
    finally:
        db.close()
    yield


app = FastAPI(title="FacePay — yuz orqali to'lov tizimi", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and int(cl) > MAX_BODY_BYTES:
        return JSONResponse({"error": "sorov_juda_katta"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    return response


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError):
    return JSONResponse({"error": exc.code}, status_code=exc.status)


@app.exception_handler(BiometricError)
async def biometric_error_handler(_: Request, exc: BiometricError):
    return JSONResponse({"error": exc.code}, status_code=422)


# ---------------- Autentifikatsiya ----------------

class SignedRequest:
    def __init__(self, terminal: Terminal, body: bytes):
        self.terminal = terminal
        self.body = body

    def parse(self, model: type[BaseModel]):
        try:
            return model.model_validate_json(self.body or b"{}")
        except ValidationError as e:
            raise HTTPException(422, detail=[{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()])


async def signed_terminal(
    request: Request,
    db: Session = Depends(get_session),
    x_terminal_id: str = Header(...),
    x_timestamp: str = Header(...),
    x_nonce: str = Header(...),
    x_signature: str = Header(...),
) -> SignedRequest:
    core = get_core()
    if not core.rate_limiter.allow(f"t:{x_terminal_id}"):
        raise HTTPException(429, "juda_kop_sorov")
    terminal = db.get(Terminal, x_terminal_id)
    if not terminal or not terminal.active:
        raise HTTPException(401, "terminal_nomalum")
    body = await request.body()
    try:
        verify_request(terminal.public_key_b64, request.method, request.url.path, x_timestamp, x_nonce,
                       body, x_signature, terminal.id, core.nonces, get_settings().request_max_skew_seconds)
    except SignatureError as e:
        audit(db, f"terminal:{terminal.id}", "auth_failed", reason=str(e))
        db.commit()
        raise HTTPException(401, "imzo_xatosi")
    return SignedRequest(terminal, body)


def require_role(req: SignedRequest, role: str) -> None:
    if req.terminal.role != role:
        raise HTTPException(403, "ruxsat_yoq")


def require_admin(x_admin_key: str = Header(...)) -> None:
    expected = get_settings().admin_api_key
    if not expected or not hmac.compare_digest(x_admin_key.encode(), expected.encode()):
        raise HTTPException(401, "admin_emas")


def consume_challenge(db: Session, terminal: Terminal, data: CaptureIn) -> CaptureResult:
    """Sinov bir martalik: muvaffaqiyatsiz bo'lsa ham qayta ishlatib bo'lmaydi."""
    ch = db.get(Challenge, data.challenge_id)
    if not ch or ch.terminal_id != terminal.id or ch.used or ch.expires_at < utcnow():
        raise HTTPException(400, "sinov_yaroqsiz")
    ch.used = True
    db.commit()
    try:
        return process_capture(data.frames, data.timestamps_ms, ch.steps)
    except BiometricError as e:
        audit(db, f"terminal:{terminal.id}", "biometric_rejected", reason=e.code.split(":")[0])
        db.commit()
        raise


# ---------------- Terminal endpointlari ----------------

@app.get("/", include_in_schema=False)
def web_terminal():
    """Brauzer terminali: kamera, to'lov, ro'yxatdan o'tish, karta ulash."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "templates": len(get_core().gallery)}


@app.post("/v1/liveness/challenge")
def create_challenge(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    s = get_settings()
    eye_mouth = getattr(get_analyzer(), "supports_eye_mouth", False)
    steps = generate_challenge(s.challenge_steps, eye_mouth)
    ch = Challenge(terminal_id=req.terminal.id, steps=steps,
                   expires_at=utcnow() + timedelta(seconds=s.challenge_ttl_seconds))
    db.add(ch)
    db.commit()
    return {"challenge_id": ch.id, "steps": steps, "instructions": [ACTION_TEXT_UZ[a] for a in steps],
            "expires_in": s.challenge_ttl_seconds}


@app.post("/v1/enroll", status_code=201)
def enroll(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(EnrollIn)
    capture = consume_challenge(db, req.terminal, data)
    user = users.enroll(db, req.terminal, data.phone, data.full_name, data.pin, data.consent, capture)
    return {"user_id": user.id, "templates": len(user.templates)}


@app.post("/v1/templates/variant", status_code=201)
def add_variant(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(VariantIn)
    capture = consume_challenge(db, req.terminal, data)
    t = users.add_variant(db, req.terminal, data.phone, data.pin, data.label, capture)
    return {"template_id": t.id, "label": t.label}


@app.post("/v1/users/reset-pin")
def reset_pin(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(PinResetIn)
    capture = consume_challenge(db, req.terminal, data)
    users.reset_pin(db, req.terminal, data.phone, data.new_pin, capture)
    return {"reset": True}


@app.post("/v1/users/delete")
def delete_user(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(DeleteIn)
    capture = consume_challenge(db, req.terminal, data)
    users.delete_user_data(db, req.terminal, data.phone, data.pin, capture)
    return {"deleted": True}


def _tx_out(tx, info: dict | None = None) -> dict:
    out = {"transaction_id": tx.id, "status": tx.status, "amount": tx.amount}
    if tx.status == "declined":
        out["reason"] = tx.decline_reason
    if tx.method:
        out["method"] = tx.method
    if tx.funding:
        out["funding"] = tx.funding
    if info:
        out.update(info)
    return out


@app.post("/v1/payments")
def create_payment(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "payment")
    data = req.parse(PaymentIn)
    capture = consume_challenge(db, req.terminal, data)
    tx, info = payments.create_payment(db, req.terminal, data.idempotency_key, data.amount, capture)
    return _tx_out(tx, info)


@app.post("/v1/payments/{tx_id}/pin")
def confirm_pin(tx_id: str, req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "payment")
    data = req.parse(PinIn)
    return _tx_out(payments.confirm_pin(db, req.terminal, tx_id, data.pin))


# ---------------- Bank kartalari ----------------
# Faqat ro'yxatga olish terminali (bank filiali / operator) orqali. Karta raqami protsessingga
# uzatiladi va serverda saqlanmaydi. Prod'da karta kiritish protsessingning o'z formasi/SDK si
# orqali bo'lsa, server karta raqamini umuman ko'rmaydi (PCI DSS doirasi kichrayadi).

def _card_out(c) -> dict:
    return {"card_id": c.id, "masked": c.masked, "verified": c.verified, "is_default": c.is_default}


@app.post("/v1/cards", status_code=201)
def add_card(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(CardAddIn)
    card, phone_hint = cards.add_card(db, req.terminal, data.phone, data.pin, data.number, data.expire)
    return {**_card_out(card), "sms_sent_to": phone_hint, "demo": card.provider == "mock"}


@app.post("/v1/cards/{card_id}/verify")
def verify_card(card_id: str, req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(CardVerifyIn)
    return _card_out(cards.verify_card(db, req.terminal, card_id, data.phone, data.code))


@app.post("/v1/cards/list")
def list_cards(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(PhonePinIn)
    return {"cards": [_card_out(c) for c in cards.list_cards(db, data.phone, data.pin)]}


@app.post("/v1/cards/{card_id}/default")
def default_card(card_id: str, req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(PhonePinIn)
    return _card_out(cards.set_default(db, req.terminal, card_id, data.phone, data.pin))


@app.post("/v1/cards/{card_id}/delete")
def delete_card(card_id: str, req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "enroll")
    data = req.parse(PhonePinIn)
    cards.remove_card(db, req.terminal, card_id, data.phone, data.pin)
    return {"deleted": True}


# ---------------- Admin endpointlari ----------------

@app.post("/v1/admin/merchants", status_code=201, dependencies=[Depends(require_admin)])
def create_merchant(data: MerchantIn, db: Session = Depends(get_session)):
    m = Merchant(name=data.name)
    db.add(m)
    audit(db, "admin", "merchant_created", merchant_id=m.id)
    db.commit()
    return {"merchant_id": m.id}


@app.post("/v1/admin/terminals", status_code=201, dependencies=[Depends(require_admin)])
def create_terminal(data: TerminalIn, db: Session = Depends(get_session)):
    if data.role == "payment" and not (data.merchant_id and db.get(Merchant, data.merchant_id)):
        raise HTTPException(400, "sotuvchi_topilmadi")
    t = Terminal(name=data.name, merchant_id=data.merchant_id, public_key_b64=data.public_key_b64, role=data.role)
    db.add(t)
    db.flush()
    audit(db, "admin", "terminal_created", terminal_id=t.id, role=data.role)
    db.commit()
    return {"terminal_id": t.id}


@app.post("/v1/admin/topup", dependencies=[Depends(require_admin)])
def topup(data: TopUpIn, db: Session = Depends(get_session)):
    """Demo uchun: balansni to'ldirish. Real tizimda bank/karta integratsiyasi."""
    user = users.find_user_by_phone(db, data.phone)
    if not user or user.status != "active":
        raise HTTPException(404, "topilmadi")
    user.balance += data.amount
    audit(db, "admin", "topup", user_id=user.id, amount=data.amount)
    db.commit()
    return {"balance": user.balance}


@app.get("/v1/admin/audit/verify", dependencies=[Depends(require_admin)])
def audit_verify(db: Session = Depends(get_session)):
    ok, broken = verify_audit_chain(db)
    return {"intact": ok, "first_broken_id": broken}


@app.post("/v1/admin/gallery/reload", dependencies=[Depends(require_admin)])
def gallery_reload(db: Session = Depends(get_session)):
    return {"templates": get_core().load_gallery(db)}
