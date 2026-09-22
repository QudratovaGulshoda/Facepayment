"""Karta protsessingi (Uzcard / Humo) bilan integratsiya.

Karta raqami (PAN) serverda SAQLANMAYDI. Protsessing kartani tokenga aylantiradi,
bazaga faqat shifrlangan token va niqoblangan raqam yoziladi.

Kartani ulash:
    1. create_card(raqam, amal_muddati)  -> token
    2. send_code(token)                  -> kartaga bog'langan telefonga SMS
    3. verify(token, kod)                -> karta egasi tasdiqladi
To'lov:
    charge(token, summa, buyurtma_id)

Ikki xil implementatsiya bor:
  - MockGateway  — demo va testlar uchun (SMS kod: 666666, "0000" bilan tugagan karta — mablag' yetarli emas)
  - PaymeGateway — Payme Subscribe API (cards.* va receipts.*). Uni ishlatish uchun Payme'dan
    merchant ID va kalit olinadi (test muhit: checkout.test.paycom.uz)
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings


class GatewayError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class CardInfo:
    token: str
    masked: str        # 8600 **** **** 1234
    phone_hint: str    # SMS yuborilgan telefon (niqoblangan), masalan +99890*****67


class CardGateway(Protocol):
    name: str

    def create_card(self, number: str, expire: str) -> CardInfo: ...
    def send_code(self, token: str) -> str: ...
    def verify(self, token: str, code: str) -> None: ...
    def charge(self, token: str, amount: int, order_id: str) -> str: ...
    def remove(self, token: str) -> None: ...


def luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def validate_card(number: str, expire: str) -> str:
    number = number.replace(" ", "").replace("-", "")
    if not (number.isdigit() and len(number) == 16):
        raise GatewayError("karta_raqami_notogri")
    if not number.startswith(("8600", "5614", "9860")):  # Uzcard, Humo
        raise GatewayError("faqat_uzcard_humo")
    if not luhn_ok(number):
        raise GatewayError("karta_raqami_notogri")
    if not (expire.isdigit() and len(expire) == 4 and 1 <= int(expire[:2]) <= 12):
        raise GatewayError("muddat_notogri")  # format: MMYY
    return number


def mask(number: str) -> str:
    return f"{number[:4]} **** **** {number[-4:]}"


class MockGateway:
    """Demo protsessing. Hech qanday pul yechilmaydi. Holatsiz: server qayta ishga tushsa ham
    tokenlar ishlayveradi (tasdiqlanganlik holati bazadagi Card.verified da)."""

    name = "mock"
    TEST_CODE = "666666"

    def __init__(self):
        self._ids = itertools.count(1)

    @staticmethod
    def _check(token: str) -> None:
        if not token.startswith("mock_"):
            raise GatewayError("karta_topilmadi")

    def create_card(self, number: str, expire: str) -> CardInfo:
        number = validate_card(number, expire)
        digest = hashlib.sha256(f"{number}{expire}{next(self._ids)}".encode()).hexdigest()[:32]
        flag = "nf" if number.endswith("0000") else "ok"
        return CardInfo(f"mock_{digest}_{flag}", mask(number), "")

    def send_code(self, token: str) -> str:
        # Demo: SMS yuborilmaydi. Bo'sh qiymat -> servis foydalanuvchining o'z raqamini ko'rsatadi
        self._check(token)
        return ""

    def verify(self, token: str, code: str) -> None:
        self._check(token)
        if code != self.TEST_CODE:
            raise GatewayError("sms_kod_notogri")

    def charge(self, token: str, amount: int, order_id: str) -> str:
        self._check(token)
        if token.endswith("_nf"):
            raise GatewayError("kartada_mablag_yetarli_emas")
        return f"mock_tx_{order_id}"

    def remove(self, token: str) -> None:
        self._check(token)


class PaymeGateway:
    """Payme Subscribe API (JSON-RPC). Summalar tiyinda yuboriladi (1 so'm = 100 tiyin).
    Metod nomlari va maydonlarni Payme hujjatlari bilan solishtirib tekshiring:
    https://developer.help.paycom.uz
    """

    name = "payme"

    def __init__(self, url: str, merchant_id: str, key: str):
        self._url = url
        self._id = merchant_id
        self._key = key
        self._http = httpx.Client(timeout=30)
        self._seq = itertools.count(1)

    def _call(self, method: str, params: dict, with_key: bool) -> dict:
        auth = f"{self._id}:{self._key}" if with_key else self._id
        r = self._http.post(self._url, json={"id": next(self._seq), "method": method, "params": params},
                            headers={"X-Auth": auth})
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            # Xabar matnini (karta ma'lumoti bo'lishi mumkin) tashqariga chiqarmaymiz
            raise GatewayError(f"payme_{data['error'].get('code', 'xato')}")
        return data["result"]

    def create_card(self, number: str, expire: str) -> CardInfo:
        number = validate_card(number, expire)
        res = self._call("cards.create", {"card": {"number": number, "expire": expire}, "save": True}, False)
        card = res["card"]
        return CardInfo(card["token"], card.get("number", mask(number)), "")

    def send_code(self, token: str) -> str:
        return self._call("cards.get_verify_code", {"token": token}, False).get("phone", "")

    def verify(self, token: str, code: str) -> None:
        res = self._call("cards.verify", {"token": token, "code": code}, False)
        if not res.get("card", {}).get("verify"):
            raise GatewayError("sms_kod_notogri")

    def charge(self, token: str, amount: int, order_id: str) -> str:
        receipt = self._call("receipts.create", {"amount": amount * 100, "account": {"order_id": order_id}}, True)
        rid = receipt["receipt"]["_id"]
        paid = self._call("receipts.pay", {"id": rid, "token": token}, True)
        if paid["receipt"].get("state") != 4:  # 4 = to'langan
            raise GatewayError("tolov_otmadi")
        return rid

    def remove(self, token: str) -> None:
        self._call("cards.remove", {"token": token}, False)


_gateway: CardGateway | None = None


def get_gateway() -> CardGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        if s.payment_gateway == "payme":
            _gateway = PaymeGateway(s.payme_url, s.payme_merchant_id, s.payme_key)
        else:
            _gateway = MockGateway()
    return _gateway


def set_gateway(g: CardGateway | None) -> None:
    global _gateway
    _gateway = g
