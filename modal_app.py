"""FacePay'ni Modal'ga (serverless, bepul $30/oy kredit) joylash.

    .venv/bin/python scripts/deploy_modal.py      # kalitlarni yaratadi va joylaydi

Server faqat so'rov kelganda ishga tushadi va 10 daqiqa tinch turgandan keyin o'chadi
(birinchi so'rov ~30-60 s). Baza Modal Volume'da (doimiy) saqlanadi.
Faqat 1 ta konteyner: galereya va nonce keshi jarayon xotirasida.
"""
import modal

APP_DIR = "/root/app"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("build-essential", "g++", "curl", "libgl1", "libglib2.0-0", "libgles2", "libegl1")
    .pip_install_from_requirements("requirements.txt")
    # insightface o'zi bilan opencv-python ham tortadi — mediapipe talab qiladigan contrib bilan to'qnashadi
    .run_commands(
        "pip uninstall -y opencv-python opencv-python-headless",
        "pip install --force-reinstall --no-deps opencv-contrib-python==5.0.0.93",
    )
    .add_local_file("scripts/download_models.sh", f"{APP_DIR}/scripts/download_models.sh", copy=True)
    .run_commands(
        f"cd {APP_DIR} && bash scripts/download_models.sh",
        "python -c \"from insightface.app import FaceAnalysis; FaceAnalysis(name='buffalo_l', allowed_modules=['detection'])\"",
    )
    .add_local_dir("app", f"{APP_DIR}/app", ignore=["**/__pycache__"])
)

app = modal.App("facepay", image=image)
data = modal.Volume.from_name("facepay-data", create_if_missing=True)


@app.function(
    secrets=[modal.Secret.from_name("facepay-secrets")],
    volumes={"/data": data},
    cpu=2.0,
    memory=2048,
    max_containers=1,
    scaledown_window=600,
    timeout=180,
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app()
def web():
    import os
    import sys

    os.chdir(APP_DIR)  # models/ va static/ nisbiy yo'llari uchun
    sys.path.insert(0, APP_DIR)
    from app.main import app as fastapi_app

    return fastapi_app
