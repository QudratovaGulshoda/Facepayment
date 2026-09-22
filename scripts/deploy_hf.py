"""FacePay'ni Hugging Face Spaces'ga (bepul) joylash.

    .venv/bin/python scripts/deploy_hf.py                        # token so'raladi
    HF_TOKEN=hf_xxx .venv/bin/python scripts/deploy_hf.py
    .venv/bin/python scripts/deploy_hf.py --database-url "postgresql://..."   # doimiy baza (Neon)

Nima qiladi:
  1. Space yaratadi (Docker), bor bo'lsa yangilaydi
  2. Shifrlash kalitlari va admin kalitini BIR MARTA yaratadi (.deploy_secrets.json, 0600)
     va Space "secrets" bo'limiga yozadi — ular kodda ham, repoda ham yo'q
  3. Kodni yuklaydi (.env, .venv, baza, modellar yuklanmaydi)
  4. Manzil va admin kalitini chiqaradi
"""
import argparse
import base64
import getpass
import json
import os
import re
import secrets
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, get_token

ROOT = Path(__file__).resolve().parent.parent
SECRETS_FILE = ROOT / ".deploy_secrets.json"

INCLUDE_DIRS = ["app", "scripts", "terminal_client", "tests"]
INCLUDE_FILES = ["Dockerfile", ".dockerignore", "requirements.txt", ".env.example", "facepay"]
SKIP = re.compile(r"(__pycache__|\.pyc$|\.db$)")

SPACE_HEADER = """---
title: FacePay
emoji: 🙂
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Yuz orqali to'lov tizimi (diplom loyihasi)
---

"""


def load_secrets() -> dict:
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text())
    k = lambda: base64.b64encode(os.urandom(32)).decode()  # noqa: E731
    data = {
        "FACEPAY_MASTER_KEY_B64": k(),
        "FACEPAY_MASTER_KEY_VERSION": "1",
        "FACEPAY_LOOKUP_KEY_B64": k(),
        "FACEPAY_TEMPLATE_TRANSFORM_SEED_B64": k(),
        "FACEPAY_ADMIN_API_KEY": secrets.token_urlsafe(24),
        "FACEPAY_PAYMENT_GATEWAY": "mock",
    }
    SECRETS_FILE.write_text(json.dumps(data, indent=2))
    SECRETS_FILE.chmod(0o600)
    return data


def collect_files() -> list[CommitOperationAdd]:
    ops = []
    for d in INCLUDE_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.is_file() and not SKIP.search(str(p)):
                ops.append(CommitOperationAdd(path_in_repo=str(p.relative_to(ROOT)), path_or_fileobj=str(p)))
    for f in INCLUDE_FILES:
        ops.append(CommitOperationAdd(path_in_repo=f, path_or_fileobj=str(ROOT / f)))
    readme = SPACE_HEADER + (ROOT / "README.md").read_text()
    ops.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=readme.encode()))
    return ops


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default="facepay")
    ap.add_argument("--database-url", default=os.environ.get("FACEPAY_DATABASE_URL", ""))
    a = ap.parse_args()

    # Tartib: HF_TOKEN -> ~/.cache/huggingface/token -> so'rash
    token = os.environ.get("HF_TOKEN") or get_token() or getpass.getpass("Hugging Face token (hf_...): ").strip()
    api = HfApi(token=token)
    user = api.whoami()["name"]
    repo_id = f"{user}/{a.space}"
    print(f"Space: {repo_id}")

    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)

    env = load_secrets()
    if a.database_url:
        env["FACEPAY_DATABASE_URL"] = a.database_url
    for key, value in env.items():
        api.add_space_secret(repo_id, key, value)
    print(f"Maxfiy kalitlar Space secrets ga yozildi ({len(env)} ta)")

    api.create_commit(repo_id, repo_type="space", operations=collect_files(),
                      commit_message="FacePay: joylash")
    host = re.sub(r"[^a-z0-9-]", "-", f"{user}-{a.space}".lower())
    print("\nYuklandi. Build 10-15 daqiqa davom etadi (modellar yuklanadi).")
    print(f"  Build holati:  https://huggingface.co/spaces/{repo_id}")
    print(f"  Terminal:      https://{host}.hf.space")
    print(f"  Admin kaliti:  {env['FACEPAY_ADMIN_API_KEY']}")
    if not a.database_url:
        print("\nDiqqat: doimiy baza ulanmagan — Space qayta ishga tushsa (masalan 48 soat tinch turgandan keyin)\n"
              "ro'yxatdan o'tganlar o'chadi. Doimiy saqlash uchun --database-url (Neon Postgres) bering.")


if __name__ == "__main__":
    sys.exit(main())
