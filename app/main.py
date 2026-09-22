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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.biometrics.analyzer import get_analyzer
from app.biometrics.liveness import ACTION_TEXT_UZ, generate_challenge
from app.biometrics.pipeline import BiometricError, CaptureResult, process_capture
from app.config import get_settings
from app.core import audit, get_core, verify_audit_chain
from app.db import Challenge, Merchant, Terminal, get_session, session_factory, utcnow
from app.schemas import (AmountIn, CaptureIn, CardAddIn, CardIdPhonePinIn, CardVerifyIn, CardVerifyPortalIn, DeleteIn,
                         EnrollIn, MerchantIn, PairIn, PaymentIn, PhonePinIn, PinIn, PinResetIn, TerminalIn,
                         TopUpIn, VariantIn)
from app.security.terminal_auth import SignatureError, verify_request
from app.services import cards, checkout, payments, users
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


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError):
    # Kiritilgan qiymat (karta raqami, PIN) javobda qaytarilmaydi — faqat maydon nomi va sabab
    return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]}, status_code=422)


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
    if not terminal or not terminal.active or terminal.role == PORTAL_ROLE:
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
def customer_site():
    """Mijoz sayti: ro'yxatdan o'tish, karta, PIN tiklash, tarix."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/kassa", include_in_schema=False)
def merchant_kassa():
    """Sotuvchi kassasi: summa kiritadi va natijani ko'radi (kamerasiz)."""
    return FileResponse(STATIC_DIR / "kassa.html")


@app.get("/ekran", include_in_schema=False)
def customer_display():
    """Mijozga qaragan ekran: summa, kamera, PIN."""
    return FileResponse(STATIC_DIR / "ekran.html")


STATIC_FILES = {"common.js": "text/javascript", "style.css": "text/css"}


@app.get("/static/{name}", include_in_schema=False)
def static_file(name: str):
    if name not in STATIC_FILES:  # faqat ruxsat etilgan fayllar (path traversal yo'q)
        raise HTTPException(404)
    return FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name])


@app.get("/health")
def health():
    return {"status": "ok", "templates": len(get_core().gallery)}


def _new_challenge(db: Session, terminal: Terminal) -> dict:
    s = get_settings()
    eye_mouth = getattr(get_analyzer(), "supports_eye_mouth", False)
    steps = generate_challenge(s.challenge_steps, eye_mouth)
    ch = Challenge(terminal_id=terminal.id, steps=steps,
                   expires_at=utcnow() + timedelta(seconds=s.challenge_ttl_seconds))
    db.add(ch)
    db.commit()
    return {"challenge_id": ch.id, "steps": steps, "instructions": [ACTION_TEXT_UZ[a] for a in steps],
            "expires_in": s.challenge_ttl_seconds}


@app.post("/v1/liveness/challenge")
def create_challenge(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    if req.terminal.role not in ("payment", "enroll"):  # kassada kamera yo'q
        raise HTTPException(403, "ruxsat_yoq")
    return _new_challenge(db, req.terminal)


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


@app.post("/v1/terminal/transactions")
def terminal_transactions(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "payment")
    return payments.terminal_history(db, req.terminal)


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


# ---------------- Ikki qurilmali kassa ----------------
# /ekran — mijozga qaragan qurilma (kamera terminali, role=payment)
# /kassa — sotuvchi qurilmasi (role=cashier), ekranga ulash kodi orqali ulanadi

@app.post("/v1/terminal/pairing-code")
def pairing_code(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "payment")
    return {"code": checkout.new_pairing_code(db, req.terminal), "expires_in": int(checkout.PAIRING_TTL.total_seconds())}


@app.post("/v1/terminal/pending")
def terminal_pending(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "payment")
    return {"request": checkout.pending_for_camera(db, req.terminal)}


@app.post("/v1/terminal/requests/{request_id}/pay")
def terminal_pay_request(request_id: str, req: SignedRequest = Depends(signed_terminal),
                         db: Session = Depends(get_session)):
    require_role(req, "payment")
    data = req.parse(CaptureIn)
    pr = checkout.claim_request(db, req.terminal, request_id)
    try:
        capture = consume_challenge(db, req.terminal, data)
    except Exception:
        checkout.release_request(db, pr)  # mijoz qayta urinishi mumkin
        raise
    tx, info = checkout.pay_request(db, req.terminal, pr, capture)
    return _tx_out(tx, info)


@app.post("/v1/cashier/requests")
def cashier_create_request(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "cashier")
    pr = checkout.create_request(db, req.terminal, req.parse(AmountIn).amount)
    return {"request_id": pr.id, "amount": pr.amount, "status": pr.status}


@app.post("/v1/cashier/requests/{request_id}")
def cashier_request_status(request_id: str, req: SignedRequest = Depends(signed_terminal),
                           db: Session = Depends(get_session)):
    require_role(req, "cashier")
    return checkout.request_status(db, request_id, req.terminal)


@app.post("/v1/cashier/requests/{request_id}/cancel")
def cashier_cancel(request_id: str, req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "cashier")
    checkout.cancel_request(db, request_id, req.terminal)
    return {"cancelled": True}


@app.post("/v1/cashier/transactions")
def cashier_transactions(req: SignedRequest = Depends(signed_terminal), db: Session = Depends(get_session)):
    require_role(req, "cashier")
    return checkout.cashier_history(db, req.terminal)


# ---------------- Mijoz sayti (ochiq) ----------------
# Mijoz o'z telefonidan kiradi — imzolaydigan terminal yo'q. Himoya:
#   - IP bo'yicha qattiq rate limit
#   - har bir biometrik amal liveness sinovi bilan (sinov bir martalik, 30 s)
#   - karta/tarix/variant/o'chirish — PIN bilan; PIN tiklash — yuz (1:N margin) bilan
#   - ro'yxatdan o'tishda bir yuzga bitta hisob (1:N tekshiruv)
# Prod'da qo'shimcha: telefon raqamiga SMS OTP (egasi ekanini tasdiqlash).
# Audit uchun barcha amallar bitta "web-portal" psevdo-terminaliga yoziladi (imzo bilan kirib bo'lmaydi).

PORTAL_ROLE = "portal"


def _portal_terminal(db: Session) -> Terminal:
    t = db.query(Terminal).filter(Terminal.role == PORTAL_ROLE).first()
    if not t:
        t = Terminal(name="web-portal", role=PORTAL_ROLE, public_key_b64="-", active=True)
        db.add(t)
        db.commit()
    return t


def portal_client(request: Request, db: Session = Depends(get_session)) -> Terminal:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "?")
    if not get_core().portal_limiter.allow(f"ip:{ip}"):
        raise HTTPException(429, "juda_kop_sorov")
    return _portal_terminal(db)


@app.post("/v1/pair", status_code=201)
def pair(data: PairIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    """Kassani mijoz ekraniga ulash. Ochiq, lekin IP limit + 6 xonali bir martalik kod (10 daqiqa)."""
    cashier, merchant = checkout.pair_cashier(db, data.code, data.public_key_b64, data.name)
    return {"terminal_id": cashier.id, "merchant": merchant.name if merchant else ""}


@app.post("/v1/portal/challenge")
def portal_challenge(t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    return _new_challenge(db, t)


@app.post("/v1/portal/enroll", status_code=201)
def portal_enroll(data: EnrollIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    capture = consume_challenge(db, t, data)
    user = users.enroll(db, t, data.phone, data.full_name, data.pin, data.consent, capture)
    return {"user_id": user.id, "templates": len(user.templates)}


@app.post("/v1/portal/reset-pin")
def portal_reset_pin(data: PinResetIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    capture = consume_challenge(db, t, data)
    users.reset_pin(db, t, data.phone, data.new_pin, capture)
    return {"reset": True}


@app.post("/v1/portal/variant", status_code=201)
def portal_variant(data: VariantIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    capture = consume_challenge(db, t, data)
    tpl = users.add_variant(db, t, data.phone, data.pin, data.label, capture)
    return {"template_id": tpl.id, "label": tpl.label}


@app.post("/v1/portal/delete")
def portal_delete(data: DeleteIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    capture = consume_challenge(db, t, data)
    users.delete_user_data(db, t, data.phone, data.pin, capture)
    return {"deleted": True}


@app.post("/v1/portal/cards", status_code=201)
def portal_add_card(data: CardAddIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    card, phone_hint = cards.add_card(db, t, data.phone, data.pin, data.number, data.expire)
    return {**_card_out(card), "sms_sent_to": phone_hint, "demo": card.provider == "mock"}


@app.post("/v1/portal/cards/verify")
def portal_verify_card(data: CardVerifyPortalIn, t: Terminal = Depends(portal_client),
                       db: Session = Depends(get_session)):
    return _card_out(cards.verify_card(db, t, data.card_id, data.phone, data.code))


@app.post("/v1/portal/cards/list")
def portal_list_cards(data: PhonePinIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    return {"cards": [_card_out(c) for c in cards.list_cards(db, data.phone, data.pin)]}


@app.post("/v1/portal/cards/default")
def portal_default_card(data: CardIdPhonePinIn, t: Terminal = Depends(portal_client),
                        db: Session = Depends(get_session)):
    return _card_out(cards.set_default(db, t, data.card_id, data.phone, data.pin))


@app.post("/v1/portal/cards/delete")
def portal_delete_card(data: CardIdPhonePinIn, t: Terminal = Depends(portal_client),
                       db: Session = Depends(get_session)):
    cards.remove_card(db, t, data.card_id, data.phone, data.pin)
    return {"deleted": True}


@app.post("/v1/portal/history")
def portal_history(data: PhonePinIn, t: Terminal = Depends(portal_client), db: Session = Depends(get_session)):
    return {"transactions": payments.user_history(db, data.phone, data.pin)}


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
