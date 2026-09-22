"""Modal'ga joylash: maxfiy kalitlarni Modal Secret'ga yozadi va ilovani deploy qiladi.

    .venv/bin/modal token new                 # bir marta: brauzerda GitHub orqali kirish
    .venv/bin/python scripts/deploy_modal.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_hf import load_secrets  # noqa: E402  (bir xil kalitlar fayli: .deploy_secrets.json)

MODAL = str(ROOT / ".venv" / "bin" / "modal")


def main() -> None:
    env = load_secrets()
    env.setdefault("FACEPAY_DATABASE_URL", "sqlite:////data/facepay.db")
    # Kalitlar ekranga chiqarilmaydi (stdout yopiq)
    subprocess.run([MODAL, "secret", "create", "--force", "facepay-secrets",
                    *[f"{k}={v}" for k, v in env.items()]], check=True, cwd=ROOT,
                   stdout=subprocess.DEVNULL)
    print("Maxfiy kalitlar Modal Secret'ga yozildi")
    subprocess.run([MODAL, "deploy", "modal_app.py"], check=True, cwd=ROOT)
    print(f"\nAdmin kaliti: {env['FACEPAY_ADMIN_API_KEY']}")


if __name__ == "__main__":
    main()
