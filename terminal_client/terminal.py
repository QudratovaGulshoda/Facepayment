"""Demo terminal (metro turniketi / kassa) — noutbuk kamerasi bilan.

Birinchi marta:
    python -m terminal_client.terminal setup --role enroll      # ro'yxatga olish punkti
    python -m terminal_client.terminal setup --role payment     # to'lov terminali

Ishlatish:
    python -m terminal_client.terminal enroll --phone +998901234567 --name "Ali Valiyev"
    python -m terminal_client.terminal pay --amount 1700
    python -m terminal_client.terminal variant --phone +998901234567 --label makiyaj
    python -m terminal_client.terminal delete --phone +998901234567
    python -m terminal_client.terminal topup --phone +998901234567 --amount 100000
    python -m terminal_client.terminal card --phone +998901234567     # bank kartasini ulash
    python -m terminal_client.terminal cards --phone +998901234567    # kartalar ro'yxati

Maxfiylik: kadrlar faqat xotirada, diskka yozilmaydi; server ham ularni saqlamaydi.
"""
import argparse
import base64
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path

import cv2
import httpx

from terminal_client.signing import TerminalClient, generate_keypair, load_key

CONF_DIR = Path.home() / ".facepay_terminal"
SERVER = os.environ.get("FACEPAY_SERVER", "http://127.0.0.1:8000")
MAX_FRAMES = 38
JPEG_QUALITY = 85


def conf_path(role: str) -> Path:
    return CONF_DIR / f"{role}.json"


def setup(role: str, admin_key: str) -> None:
    CONF_DIR.mkdir(mode=0o700, exist_ok=True)
    key_path = CONF_DIR / f"{role}.pem"
    pub = generate_keypair(key_path)
    headers = {"X-Admin-Key": admin_key}
    merchant_id = None
    if role == "payment":
        r = httpx.post(f"{SERVER}/v1/admin/merchants", json={"name": "Toshkent metropoliteni"}, headers=headers)
        r.raise_for_status()
        merchant_id = r.json()["merchant_id"]
    r = httpx.post(f"{SERVER}/v1/admin/terminals", headers=headers,
                   json={"name": f"demo-{role}", "merchant_id": merchant_id, "public_key_b64": pub, "role": role})
    r.raise_for_status()
    conf_path(role).write_text(json.dumps({"terminal_id": r.json()["terminal_id"], "key": str(key_path)}))
    print(f"Terminal ro'yxatdan o'tdi: {r.json()['terminal_id']}")


def client(role: str) -> TerminalClient:
    if not conf_path(role).exists():
        sys.exit(f"Avval: python -m terminal_client.terminal setup --role {role}")
    c = json.loads(conf_path(role).read_text())
    return TerminalClient(SERVER, c["terminal_id"], load_key(Path(c["key"])))


def capture(instructions: list[str], seconds_per_step: float = 2.0) -> tuple[list[str], list[int]]:
    """Kameradan kadrlar oladi va foydalanuvchiga ko'rsatmalarni ekranda chiqaradi."""
    cam = cv2.VideoCapture(0)
    if not cam.isOpened():
        sys.exit("Kamera ochilmadi")
    frames, stamps = [], []
    plan = ["Kameraga to'g'ri qarang"] + instructions + ["Kameraga to'g'ri qarang"]
    fps = min(8.0, MAX_FRAMES / (len(plan) * seconds_per_step))  # kadrlar barcha bosqichlarga yetsin
    try:
        for text in plan:
            end = time.time() + seconds_per_step
            last = 0.0
            while time.time() < end:
                ok, img = cam.read()
                if not ok:
                    continue
                now = time.time()
                if now - last >= 1.0 / fps and len(frames) < MAX_FRAMES:
                    last = now
                    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                    frames.append(base64.b64encode(jpg.tobytes()).decode())
                    stamps.append(int(now * 1000))
                view = img.copy()
                cv2.putText(view, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
                cv2.imshow("FacePay", view)
                if cv2.waitKey(1) == 27:
                    sys.exit("Bekor qilindi")
    finally:
        cam.release()
        cv2.destroyAllWindows()
    return frames, stamps


def run_with_challenge(c: TerminalClient, path: str, payload: dict) -> httpx.Response:
    r = c.post("/v1/liveness/challenge", {})
    r.raise_for_status()
    ch = r.json()
    print("Ko'rsatmalar:", " -> ".join(ch["instructions"]))
    frames, stamps = capture(ch["instructions"])
    return c.post(path, {**payload, "challenge_id": ch["challenge_id"], "frames": frames, "timestamps_ms": stamps})


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--role", choices=["enroll", "payment"], required=True)
    e = sub.add_parser("enroll")
    e.add_argument("--phone", required=True)
    e.add_argument("--name", required=True)
    pay = sub.add_parser("pay")
    pay.add_argument("--amount", type=int, required=True)
    v = sub.add_parser("variant")
    v.add_argument("--phone", required=True)
    v.add_argument("--label", default="makiyaj")
    d = sub.add_parser("delete")
    d.add_argument("--phone", required=True)
    cd = sub.add_parser("card")
    cd.add_argument("--phone", required=True)
    cl = sub.add_parser("cards")
    cl.add_argument("--phone", required=True)
    t = sub.add_parser("topup")
    t.add_argument("--phone", required=True)
    t.add_argument("--amount", type=int, required=True)
    a = p.parse_args()

    if a.cmd == "topup":
        # Demo: admin kaliti loyiha ichidagi .env dan olinadi
        env = dict(line.split("=", 1) for line in Path(".env").read_text().splitlines() if "=" in line)
        r = httpx.post(f"{SERVER}/v1/admin/topup", json={"phone": a.phone, "amount": a.amount},
                       headers={"X-Admin-Key": env["FACEPAY_ADMIN_API_KEY"]})
        print(r.status_code, r.json())
        return

    if a.cmd == "card":
        c = client("enroll")
        print("Karta raqami serverda saqlanmaydi — faqat protsessing tokeni.")
        number = getpass.getpass("Karta raqami (16 raqam, ekranda ko'rinmaydi): ").replace(" ", "")
        expire = input("Amal qilish muddati (MMYY, masalan 0829): ").strip()
        pin = getpass.getpass("FacePay PIN: ")
        r = c.post("/v1/cards", {"phone": a.phone, "pin": pin, "number": number, "expire": expire})
        if r.status_code != 201:
            print(r.status_code, r.json())
            return
        card = r.json()
        print(f"{card['masked']} — SMS kod yuborildi: {card['sms_sent_to']}")
        for _ in range(3):
            r = c.post(f"/v1/cards/{card['card_id']}/verify", {"phone": a.phone, "code": input("SMS kod: ").strip()})
            if r.status_code == 200 or r.json().get("error") == "sms_urinishlar_tugadi":
                break
            print("Kod noto'g'ri, qayta kiriting.")
        print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))
        return
    if a.cmd == "cards":
        r = client("enroll").post("/v1/cards/list", {"phone": a.phone, "pin": getpass.getpass("PIN: ")})
        print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))
        return
    if a.cmd == "setup":
        setup(a.role, getpass.getpass("Admin kalit: "))
        return
    if a.cmd == "enroll":
        print("Shaxsga doir ma'lumotlar to'g'risidagi Qonunga muvofiq yuz shabloningiz shifrlangan holda\n"
              "saqlanadi, rasmingiz saqlanmaydi. Istalgan vaqtda o'chirishingiz mumkin.")
        consent = input("Rozimisiz? (ha/yo'q): ").strip().lower() == "ha"
        pin = getpass.getpass("Yangi PIN (4-6 raqam): ")
        r = run_with_challenge(client("enroll"), "/v1/enroll",
                               {"phone": a.phone, "full_name": a.name, "pin": pin, "consent": consent})
    elif a.cmd == "variant":
        pin = getpass.getpass("PIN: ")
        r = run_with_challenge(client("enroll"), "/v1/templates/variant",
                               {"phone": a.phone, "pin": pin, "label": a.label})
    elif a.cmd == "delete":
        pin = getpass.getpass("PIN: ")
        r = run_with_challenge(client("enroll"), "/v1/users/delete", {"phone": a.phone, "pin": pin})
    else:
        c = client("payment")
        r = run_with_challenge(c, "/v1/payments", {"amount": a.amount, "idempotency_key": uuid.uuid4().hex})
        if r.status_code == 200 and r.json().get("status") == "pending_pin":
            print(f"Mijoz: {r.json()['customer']} — PIN talab qilinadi")
            r = c.post(f"/v1/payments/{r.json()['transaction_id']}/pin", {"pin": getpass.getpass("PIN: ")})
    print(r.status_code, json.dumps(r.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
