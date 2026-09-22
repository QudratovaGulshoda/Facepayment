"""To'liq oqim: terminal ro'yxati -> yuz bilan ro'yxatdan o'tish -> to'lov -> PIN -> hujumlar."""
import base64
import re
import uuid

import cv2
import numpy as np
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import db as dbmod
from app.biometrics import analyzer as analyzer_mod
from app.biometrics import pipeline as pipeline_mod
from app.biometrics.analyzer import Face
from app.core import reset_core
from terminal_client.signing import TerminalClient, public_key_b64
from tests.conftest import noisy, random_unit

ADMIN = {"X-Admin-Key": "test-admin-key"}
W, H = 640, 480


class MockAnalyzer:
    """Kadrning (0,0) pikselidagi ID bo'yicha oldindan yozilgan yuz(lar)ni qaytaradi."""

    supports_eye_mouth = True

    def __init__(self):
        self.specs: dict[int, list[dict]] = {}
        self._next = 1
        self._rng = np.random.default_rng(0)

    def frame(self, faces: list[dict]) -> str:
        fid = self._next
        self._next += 1
        self.specs[fid] = faces
        small = self._rng.integers(40, 216, size=(H // 8, W // 8, 3), dtype=np.uint8)
        img = cv2.resize(small, (W, H), interpolation=cv2.INTER_NEAREST)  # teksturali, lekin ixcham
        img[:8, :8] = (fid % 256, fid // 256, 7)
        ok, png = cv2.imencode(".png", img)
        return base64.b64encode(png.tobytes()).decode()

    def analyze(self, image):
        b, g, _ = image[0, 0]
        out = []
        for k, f in enumerate(self.specs[int(b) + 256 * int(g)]):
            size = 260 if k == 0 else f.get("size", 120)
            cx = W / 2 if k == 0 else 80
            out.append(Face(bbox=np.array([cx - size / 2, 100, cx + size / 2, 100 + size], np.float32),
                            det_score=0.95, embedding=f["emb"], yaw=f.get("yaw", 0.0),
                            pitch=0.0, ear=f.get("ear", 0.3), mar=f.get("mar", 0.1),
                            extra={"spoof": f.get("spoof", 0.95)}))
        return out


class MockPassive:
    has_model = True

    def score(self, image, face):
        return face.extra["spoof"]


def poses_for(steps):
    seq = [dict(yaw=0)] * 3
    for s in steps:
        if s == "turn_left":
            seq += [dict(yaw=12), dict(yaw=28), dict(yaw=12), dict(yaw=0)]
        elif s == "turn_right":
            seq += [dict(yaw=-12), dict(yaw=-28), dict(yaw=-12), dict(yaw=0)]
        elif s == "blink":
            seq += [dict(yaw=0), dict(yaw=0, ear=0.08), dict(yaw=0)]
        elif s == "open_mouth":
            seq += [dict(yaw=0), dict(yaw=0, mar=0.65), dict(yaw=0)]
    return seq + [dict(yaw=0)] * 3


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "facepay.db"
    dbmod.init_engine(f"sqlite:///{db_path}")
    reset_core()
    mock = MockAnalyzer()
    analyzer_mod.set_analyzer(mock)
    pipeline_mod.set_passive(MockPassive())
    from app.main import app

    with TestClient(app) as http:
        r = http.post("/v1/admin/merchants", json={"name": "Metro"}, headers=ADMIN)
        merchant_id = r.json()["merchant_id"]
        clients = {}
        for role in ("enroll", "payment"):
            key = Ed25519PrivateKey.generate()
            r = http.post("/v1/admin/terminals", headers=ADMIN, json={
                "name": role, "merchant_id": merchant_id, "public_key_b64": public_key_b64(key), "role": role})
            assert r.status_code == 201, r.text
            clients[role] = TerminalClient("", r.json()["terminal_id"], key, http=http)
        yield {"http": http, "mock": mock, "enroll": clients["enroll"], "pay": clients["payment"],
               "db_path": db_path, "rng": np.random.default_rng(7)}


def capture(env, client, emb, *, static=False, extra_face=False, spoof=0.95, switch_to=None):
    ch = client.post("/v1/liveness/challenge", {}).json()
    rng = env["rng"]
    poses = [dict(yaw=0)] * 12 if static else poses_for(ch["steps"])
    frames, stamps = [], []
    for k, p in enumerate(poses):
        who = switch_to if (switch_to is not None and k >= len(poses) // 2) else emb
        faces = [{"emb": noisy(who, rng, 0.92), "spoof": spoof, **p}]
        if extra_face:
            faces.append({"emb": random_unit(rng), "size": 240})
        frames.append(env["mock"].frame(faces))
        stamps.append(1_700_000_000_000 + k * 150)
    return {"challenge_id": ch["challenge_id"], "frames": frames, "timestamps_ms": stamps}


def enroll(env, emb, phone="+998901234567", name="Gulnora Karimova", pin="4821", balance=1_000_000):
    r = env["enroll"].post("/v1/enroll", {**capture(env, env["enroll"], emb), "phone": phone,
                                          "full_name": name, "pin": pin, "consent": True})
    assert r.status_code == 201, r.text
    if balance:
        env["http"].post("/v1/admin/topup", json={"phone": phone, "amount": balance}, headers=ADMIN)
    return r.json()


def pay(env, emb, amount=1_700, **kw):
    return env["pay"].post("/v1/payments", {**capture(env, env["pay"], emb, **kw), "amount": amount,
                                            "idempotency_key": uuid.uuid4().hex})


# ---------------- Asosiy oqim ----------------

def test_metro_payment_face_only(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = pay(env, me, 1_700)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["method"] == "face"
    assert body["customer"] == "G****** K."   # ism to'liq ko'rsatilmaydi


def test_large_amount_requires_pin(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = pay(env, me, 450_000).json()
    assert r["status"] == "pending_pin"
    bad = env["pay"].post(f"/v1/payments/{r['transaction_id']}/pin", {"pin": "0000"})
    assert bad.status_code == 401
    ok = env["pay"].post(f"/v1/payments/{r['transaction_id']}/pin", {"pin": "4821"}).json()
    assert ok["status"] == "approved" and ok["method"] == "face+pin"


def test_insufficient_balance(env):
    me = random_unit(env["rng"])
    enroll(env, me, balance=1000)
    assert pay(env, me, 1_700).json()["reason"] == "mablag_yetarli_emas"


def test_idempotency_no_double_charge(env):
    me = random_unit(env["rng"])
    enroll(env, me, balance=2000)
    key = uuid.uuid4().hex
    first = env["pay"].post("/v1/payments", {**capture(env, env["pay"], me), "amount": 1700, "idempotency_key": key})
    second = env["pay"].post("/v1/payments", {**capture(env, env["pay"], me), "amount": 1700, "idempotency_key": key})
    assert first.json()["transaction_id"] == second.json()["transaction_id"]
    assert first.json()["status"] == "approved"


# ---------------- Hujumlar ----------------

def test_impostor_declined(env):
    enroll(env, random_unit(env["rng"]))
    r = pay(env, random_unit(env["rng"]))
    assert r.json()["status"] == "declined" and r.json()["reason"] == "yuz_tanilmadi"


def test_photo_attack_rejected(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = pay(env, me, static=True)
    assert r.status_code == 422 and r.json()["error"].startswith("liveness_faol")


def test_screen_replay_rejected_by_passive(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = pay(env, me, spoof=0.2)
    assert r.status_code == 422 and r.json()["error"] == "liveness_passiv"


def test_face_switch_mid_capture_rejected(env):
    """Hujumchi harakatlarni o'zi bajaradi, keyin qurbonning rasmini ko'rsatadi."""
    victim, attacker = random_unit(env["rng"]), random_unit(env["rng"])
    enroll(env, victim)
    r = pay(env, attacker, switch_to=victim)
    assert r.status_code == 422 and r.json()["error"] == "kadrlarda_turli_shaxs"


def test_bystander_in_frame_rejected(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = pay(env, me, extra_face=True)
    assert r.status_code == 422 and r.json()["error"] == "bir_nechta_yuz"


def test_challenge_cannot_be_reused(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    cap = capture(env, env["pay"], me)
    r1 = env["pay"].post("/v1/payments", {**cap, "amount": 1700, "idempotency_key": uuid.uuid4().hex})
    r2 = env["pay"].post("/v1/payments", {**cap, "amount": 1700, "idempotency_key": uuid.uuid4().hex})
    assert r1.status_code == 200 and r2.status_code == 400


def test_unsigned_and_forged_requests_rejected(env):
    http = env["http"]
    assert http.post("/v1/liveness/challenge", json={}).status_code == 422  # sarlavhalar yo'q
    forged = TerminalClient("", env["pay"].terminal_id, Ed25519PrivateKey.generate(), http=http)
    assert forged.post("/v1/liveness/challenge", {}).status_code == 401


def test_payment_terminal_cannot_enroll(env):
    r = env["pay"].post("/v1/enroll", {})
    assert r.status_code == 403


def test_duplicate_face_enrollment_rejected(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = env["enroll"].post("/v1/enroll", {**capture(env, env["enroll"], me), "phone": "+998907777777",
                                          "full_name": "Boshqa Ism", "pin": "5930", "consent": True})
    assert r.status_code == 409 and r.json()["error"] == "yuz_allaqachon_royxatda"


def test_enrollment_requires_consent(env):
    r = env["enroll"].post("/v1/enroll", {**capture(env, env["enroll"], random_unit(env["rng"])),
                                          "phone": "+998901111111", "full_name": "Ali Valiyev",
                                          "pin": "5930", "consent": False})
    assert r.status_code == 400 and r.json()["error"] == "rozilik_berilmagan"


def test_pin_bruteforce_locks_account(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    for _ in range(2):  # har tranzaksiyada 3 ta urinish, jami 5 ta xato -> blok
        tx = pay(env, me, 450_000).json()["transaction_id"]
        for _ in range(3):
            env["pay"].post(f"/v1/payments/{tx}/pin", {"pin": "0000"})
    r = pay(env, me, 1_700).json()
    assert r["status"] == "declined" and r["reason"] == "vaqtincha_bloklangan"


# ---------------- Makiyaj ----------------

def test_heavy_makeup_variant(env):
    """Kuchli makiyaj ArcFace ballini pasaytirishi mumkin. Foydalanuvchi PIN bilan
    'makiyaj' shablonini qo'shadi, shundan keyin makiyajda ham to'lay oladi."""
    rng = env["rng"]
    me = random_unit(rng)
    enroll(env, me)
    makeup = noisy(me, rng, 0.42)       # makiyajli yuz: asosiy shablonga o'xshashlik past
    assert pay(env, makeup).json()["status"] == "declined"

    r = env["enroll"].post("/v1/templates/variant", {**capture(env, env["enroll"], makeup),
                                                     "phone": "+998901234567", "pin": "4821", "label": "makiyaj"})
    assert r.status_code == 201, r.text
    assert pay(env, makeup).json()["status"] == "approved"


def test_variant_requires_correct_pin(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = env["enroll"].post("/v1/templates/variant", {**capture(env, env["enroll"], me),
                                                     "phone": "+998901234567", "pin": "9999", "label": "x"})
    assert r.status_code == 401


# ---------------- Maxfiylik ----------------

def test_no_plaintext_personal_data_in_database(env):
    me = random_unit(env["rng"])
    enroll(env, me, phone="+998935554433", name="Shahnoza Rahimova")
    pay(env, me)
    raw = env["db_path"].read_bytes()
    for secret in (b"998935554433", b"Shahnoza", b"Rahimova", "Shahnoza".encode("utf-16-le")):
        assert secret not in raw
    # xom embedding ham bazada yo'q
    assert me.astype(np.float32).tobytes()[:16] not in raw


def test_right_to_erasure(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    r = env["enroll"].post("/v1/users/delete", {**capture(env, env["enroll"], me),
                                                "phone": "+998901234567", "pin": "4821"})
    assert r.status_code == 200
    with dbmod.session_factory()() as s:
        assert s.execute(text("select count(*) from face_templates")).scalar() == 0
    assert pay(env, me).json()["reason"] == "yuz_tanilmadi"


def test_audit_chain_detects_tampering(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    pay(env, me)
    assert env["http"].get("/v1/admin/audit/verify", headers=ADMIN).json()["intact"]
    with dbmod.session_factory()() as s:
        s.execute(text("update audit_log set actor='hacker' where id=2"))
        s.commit()
    r = env["http"].get("/v1/admin/audit/verify", headers=ADMIN).json()
    assert not r["intact"] and r["first_broken_id"] == 2


def test_admin_endpoints_require_key(env):
    assert env["http"].post("/v1/admin/merchants", json={"name": "X"}, headers={"X-Admin-Key": "no"}).status_code == 401


# ---------------- Bank kartasi ----------------

GOOD_CARD = "8600123456789012"
NO_FUNDS_CARD = "8600123456710000"


def link_card(env, number=GOOD_CARD, code="666666", phone="+998901234567", pin="4821"):
    r = env["enroll"].post("/v1/cards", {"phone": phone, "pin": pin, "number": number, "expire": "0829"})
    assert r.status_code == 201, r.text
    card = r.json()
    v = env["enroll"].post(f"/v1/cards/{card['card_id']}/verify", {"phone": phone, "code": code})
    return card, v


def test_payment_charged_from_card(env):
    me = random_unit(env["rng"])
    enroll(env, me, balance=0)
    card, v = link_card(env)
    assert v.status_code == 200 and v.json()["is_default"]
    r = pay(env, me, 1_700).json()
    assert r["status"] == "approved" and r["funding"] == "8600 **** **** 9012"


def test_card_needs_sms_verification(env):
    me = random_unit(env["rng"])
    enroll(env, me, balance=0)
    card, v = link_card(env, code="111111")
    assert v.status_code == 401
    # tasdiqlanmagan karta ishlatilmaydi -> balans 0 -> rad
    assert pay(env, me, 1_700).json()["reason"] == "mablag_yetarli_emas"


def test_card_removed_after_3_wrong_sms_codes(env):
    enroll(env, random_unit(env["rng"]), balance=0)
    card, _ = link_card(env, code="111111")
    for _ in range(2):
        r = env["enroll"].post(f"/v1/cards/{card['card_id']}/verify", {"phone": "+998901234567", "code": "222222"})
    assert r.json()["error"] == "sms_urinishlar_tugadi"
    lst = env["enroll"].post("/v1/cards/list", {"phone": "+998901234567", "pin": "4821"}).json()
    assert lst["cards"] == []


def test_card_insufficient_funds_declined(env):
    me = random_unit(env["rng"])
    enroll(env, me, balance=0)
    link_card(env, number=NO_FUNDS_CARD)
    assert pay(env, me, 1_700).json()["reason"] == "kartada_mablag_yetarli_emas"


def test_add_card_requires_pin(env):
    enroll(env, random_unit(env["rng"]))
    r = env["enroll"].post("/v1/cards", {"phone": "+998901234567", "pin": "9999", "number": GOOD_CARD, "expire": "0829"})
    assert r.status_code == 401


def test_invalid_card_number_rejected(env):
    enroll(env, random_unit(env["rng"]))
    r = env["enroll"].post("/v1/cards", {"phone": "+998901234567", "pin": "4821",
                                         "number": "8600123456789013", "expire": "0829"})
    assert r.status_code == 400 and r.json()["error"] == "karta_raqami_notogri"
    assert "8600123456789013" not in r.text  # xato javobida karta raqami qaytmaydi


def test_payment_terminal_cannot_add_card(env):
    r = env["pay"].post("/v1/cards", {"phone": "+998901234567", "pin": "4821", "number": GOOD_CARD, "expire": "0829"})
    assert r.status_code == 403


def test_card_number_not_stored(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    link_card(env)
    pay(env, me)
    raw = env["db_path"].read_bytes()
    assert GOOD_CARD.encode() not in raw
    # protsessing tokeni (mock_<32 hex>_ok) ham shifrlangan
    assert re.search(rb"mock_[0-9a-f]{32}_(ok|nf)", raw) is None


# ---------------- PIN tiklash ----------------

def reset_pin(env, emb, new_pin="7392", phone="+998901234567"):
    return env["enroll"].post("/v1/users/reset-pin", {**capture(env, env["enroll"], emb),
                                                      "phone": phone, "new_pin": new_pin})


def test_forgotten_pin_reset_with_face(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    assert reset_pin(env, me).status_code == 200
    tx = pay(env, me, 450_000).json()["transaction_id"]
    ok = env["pay"].post(f"/v1/payments/{tx}/pin", {"pin": "7392"}).json()
    assert ok["status"] == "approved"


def test_pin_reset_rejects_other_face(env):
    enroll(env, random_unit(env["rng"]))
    r = reset_pin(env, random_unit(env["rng"]))
    assert r.status_code == 401 and r.json()["error"] == "yuz_mos_emas"


def test_pin_reset_unlocks_account(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    for _ in range(2):
        tx = pay(env, me, 450_000).json()["transaction_id"]
        for _ in range(3):
            env["pay"].post(f"/v1/payments/{tx}/pin", {"pin": "0000"})
    assert reset_pin(env, me).status_code == 200
    assert pay(env, me, 1_700).json()["status"] == "approved"


def test_demo_card_sms_hint_is_users_own_phone(env):
    enroll(env, random_unit(env["rng"]), phone="+998948431011")
    r = env["enroll"].post("/v1/cards", {"phone": "+998948431011", "pin": "4821", "number": GOOD_CARD, "expire": "0829"})
    assert r.json()["sms_sent_to"] == "+99894*****11" and r.json()["demo"] is True


# ---------------- Mijoz sayti (ochiq portal) va kassa ----------------

def portal_capture(env, emb):
    ch = env["http"].post("/v1/portal/challenge", json={}).json()
    rng = env["rng"]
    frames, stamps = [], []
    for k, p in enumerate(poses_for(ch["steps"])):
        frames.append(env["mock"].frame([{"emb": noisy(emb, rng, 0.92), "spoof": 0.95, **p}]))
        stamps.append(1_700_000_000_000 + k * 150)
    return {"challenge_id": ch["challenge_id"], "frames": frames, "timestamps_ms": stamps}


def portal(env, path, payload):
    return env["http"].post(path, json=payload)


PHONE = "+998948431011"


def portal_enroll(env, emb, pin="4821"):
    r = portal(env, "/v1/portal/enroll", {**portal_capture(env, emb), "phone": PHONE,
                                          "full_name": "Gulshoda Qudratova", "pin": pin, "consent": True})
    assert r.status_code == 201, r.text


def test_portal_full_customer_flow(env):
    """Mijoz o'z telefonidan: ro'yxat -> karta -> (kassada to'lov) -> tarix."""
    me = random_unit(env["rng"])
    portal_enroll(env, me)
    card = portal(env, "/v1/portal/cards", {"phone": PHONE, "pin": "4821", "number": GOOD_CARD, "expire": "0829"}).json()
    v = portal(env, "/v1/portal/cards/verify", {"phone": PHONE, "code": "666666", "card_id": card["card_id"]})
    assert v.json()["is_default"]
    assert pay(env, me, 1_700).json()["status"] == "approved"   # kassa (imzolangan terminal)
    hist = portal(env, "/v1/portal/history", {"phone": PHONE, "pin": "4821"}).json()["transactions"]
    assert hist[0]["amount"] == 1700 and hist[0]["merchant"] == "Metro" and hist[0]["funding"].endswith("9012")
    cards_list = portal(env, "/v1/portal/cards/list", {"phone": PHONE, "pin": "4821"}).json()["cards"]
    assert len(cards_list) == 1


def test_portal_history_requires_pin(env):
    portal_enroll(env, random_unit(env["rng"]))
    assert portal(env, "/v1/portal/history", {"phone": PHONE, "pin": "9999"}).status_code == 401


def test_portal_reset_pin_with_face(env):
    me = random_unit(env["rng"])
    portal_enroll(env, me)
    r = portal(env, "/v1/portal/reset-pin", {**portal_capture(env, me), "phone": PHONE, "new_pin": "7392"})
    assert r.status_code == 200
    assert portal(env, "/v1/portal/history", {"phone": PHONE, "pin": "7392"}).status_code == 200
    other = portal(env, "/v1/portal/reset-pin", {**portal_capture(env, random_unit(env["rng"])), "phone": PHONE, "new_pin": "5816"})
    assert other.status_code == 401


def test_portal_validation_never_echoes_card_or_pin(env):
    r = portal(env, "/v1/portal/cards", {"phone": PHONE, "pin": "12", "number": "86001234567890129999", "expire": "0829"})
    assert r.status_code == 422
    assert "86001234567890129999" not in r.text and '"12"' not in r.text


def test_portal_rate_limited_per_ip(env):
    from app.core import get_core
    from app.security.terminal_auth import RateLimiter

    get_core().portal_limiter = RateLimiter(3)
    codes = [portal(env, "/v1/portal/challenge", {}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_portal_pseudo_terminal_cannot_sign(env):
    """Portal psevdo-terminali nomidan imzolangan so'rov yuborib bo'lmaydi."""
    portal(env, "/v1/portal/challenge", {})
    with dbmod.session_factory()() as s:
        pid = s.execute(text("select id from terminals where role='portal'")).scalar()
    fake = TerminalClient("", pid, Ed25519PrivateKey.generate(), http=env["http"])
    assert fake.post("/v1/liveness/challenge", {}).status_code == 401


def test_kassa_sees_own_transactions_and_today_total(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    pay(env, me, 1_700)
    pay(env, me, 2_300)
    pay(env, random_unit(env["rng"]), 5_000)  # rad etiladi
    h = env["pay"].post("/v1/terminal/transactions", {}).json()
    assert h["today_total"] == 4_000 and len(h["transactions"]) == 3
    assert "customer" not in h["transactions"][0] and "user_id" not in h["transactions"][0]


def test_pages_served(env):
    for path, marker in (("/", "Shaxsiy kabinet"), ("/kassa", "FacePay kassa"), ("/ekran", "Mijoz ekrani"),
                         ("/static/common.js", "Camera"), ("/static/style.css", ":root")):
        r = env["http"].get(path)
        assert r.status_code == 200 and marker in r.text, path
    assert env["http"].get("/static/..%2Fmain.py").status_code == 404


# ---------------- Ikki qurilmali kassa: sotuvchi + mijoz ekrani ----------------

def pair_cashier(env):
    """env["pay"] — mijoz ekrani (kamera terminali). Kassa ulash kodi bilan ulanadi."""
    code = env["pay"].post("/v1/terminal/pairing-code", {}).json()["code"]
    key = Ed25519PrivateKey.generate()
    r = env["http"].post("/v1/pair", json={"code": code, "public_key_b64": public_key_b64(key), "name": "Kassa 1"})
    assert r.status_code == 201, r.text
    return TerminalClient("", r.json()["terminal_id"], key, http=env["http"])


def screen_pay(env, emb, request_id):
    return env["pay"].post(f"/v1/terminal/requests/{request_id}/pay", capture(env, env["pay"], emb))


def test_two_device_checkout(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    kassa = pair_cashier(env)
    rq = kassa.post("/v1/cashier/requests", {"amount": 1_700}).json()
    assert kassa.post(f"/v1/cashier/requests/{rq['request_id']}", {}).json()["status"] == "waiting"
    pending = env["pay"].post("/v1/terminal/pending", {}).json()["request"]
    assert pending["amount"] == 1_700 and pending["merchant"] == "Metro"
    tx = screen_pay(env, me, pending["request_id"]).json()
    assert tx["status"] == "approved"
    st = kassa.post(f"/v1/cashier/requests/{rq['request_id']}", {}).json()
    assert st["status"] == "approved" and st["customer"] == "G****** K."
    assert kassa.post("/v1/cashier/transactions", {}).json()["today_total"] == 1_700
    assert env["pay"].post("/v1/terminal/pending", {}).json()["request"] is None


def test_screen_cannot_change_amount(env):
    """Mijoz ekrani so'rovga boshqa summa yuborsa ham, kassa belgilagan summa yechiladi."""
    me = random_unit(env["rng"])
    enroll(env, me)
    kassa = pair_cashier(env)
    rq = kassa.post("/v1/cashier/requests", {"amount": 1_700}).json()
    body = {**capture(env, env["pay"], me), "amount": 1}
    tx = env["pay"].post(f"/v1/terminal/requests/{rq['request_id']}/pay", body).json()
    assert tx["status"] == "approved" and tx["amount"] == 1_700


def test_large_amount_pin_on_customer_screen(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    kassa = pair_cashier(env)
    rq = kassa.post("/v1/cashier/requests", {"amount": 350_000}).json()
    tx = screen_pay(env, me, rq["request_id"]).json()
    assert tx["status"] == "pending_pin"
    assert kassa.post(f"/v1/cashier/requests/{rq['request_id']}", {}).json()["status"] == "pending_pin"
    env["pay"].post(f"/v1/payments/{tx['transaction_id']}/pin", {"pin": "4821"})
    assert kassa.post(f"/v1/cashier/requests/{rq['request_id']}", {}).json()["status"] == "approved"


def test_failed_liveness_lets_customer_retry(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    kassa = pair_cashier(env)
    rq = kassa.post("/v1/cashier/requests", {"amount": 1_700}).json()
    bad = env["pay"].post(f"/v1/terminal/requests/{rq['request_id']}/pay", capture(env, env["pay"], me, static=True))
    assert bad.status_code == 422
    assert env["pay"].post("/v1/terminal/pending", {}).json()["request"]["request_id"] == rq["request_id"]
    assert screen_pay(env, me, rq["request_id"]).json()["status"] == "approved"


def test_request_cannot_be_paid_twice(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    kassa = pair_cashier(env)
    rq = kassa.post("/v1/cashier/requests", {"amount": 1_700}).json()
    assert screen_pay(env, me, rq["request_id"]).json()["status"] == "approved"
    assert screen_pay(env, me, rq["request_id"]).status_code == 409


def test_cancelled_request_not_payable(env):
    me = random_unit(env["rng"])
    enroll(env, me)
    kassa = pair_cashier(env)
    rq = kassa.post("/v1/cashier/requests", {"amount": 1_700}).json()
    kassa.post(f"/v1/cashier/requests/{rq['request_id']}/cancel", {})
    assert env["pay"].post("/v1/terminal/pending", {}).json()["request"] is None
    assert screen_pay(env, me, rq["request_id"]).status_code == 409


def test_pairing_code_single_use_and_wrong_code(env):
    code = env["pay"].post("/v1/terminal/pairing-code", {}).json()["code"]
    k = lambda: public_key_b64(Ed25519PrivateKey.generate())  # noqa: E731
    wrong = f"{(int(code) + 1) % 10**6:06d}"
    assert env["http"].post("/v1/pair", json={"code": wrong, "public_key_b64": k()}).status_code == 400
    assert env["http"].post("/v1/pair", json={"code": code, "public_key_b64": k()}).status_code == 201
    assert env["http"].post("/v1/pair", json={"code": code, "public_key_b64": k()}).status_code == 400


def test_roles_are_separated(env):
    kassa = pair_cashier(env)
    # kassa to'g'ridan-to'g'ri to'lov qila olmaydi, mijoz ekrani so'rov yarata olmaydi
    assert kassa.post("/v1/liveness/challenge", {}).status_code == 403  # kassada kamera yo'q
    assert kassa.post("/v1/payments", {}).status_code == 403
    assert kassa.post("/v1/terminal/pending", {}).status_code == 403
    assert env["pay"].post("/v1/cashier/requests", {"amount": 100}).status_code == 403


def test_other_cashier_cannot_see_request(env):
    kassa1, kassa2 = pair_cashier(env), pair_cashier(env)
    rq = kassa1.post("/v1/cashier/requests", {"amount": 1_700}).json()
    assert kassa2.post(f"/v1/cashier/requests/{rq['request_id']}", {}).status_code == 404
