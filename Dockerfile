# Hugging Face Spaces (Docker SDK) uchun. Lokal: docker build -t facepay . && docker run -p 7860:7860 --env-file .env facepay
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential g++ curl libgl1 libglib2.0-0 libgles2 libegl1 \
    && rm -rf /var/lib/apt/lists/*

# HF Spaces konteynerni 1000-foydalanuvchi nomidan ishga tushiradi
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt \
    && pip uninstall -y opencv-python opencv-python-headless \
    && pip install --no-cache-dir --user --force-reinstall --no-deps opencv-contrib-python==5.0.0.93

# Modellarni build vaqtida yuklab olamiz — server tez ishga tushadi
COPY --chown=user scripts/download_models.sh scripts/
RUN bash scripts/download_models.sh \
    && python -c "from insightface.app import FaceAnalysis; FaceAnalysis(name='buffalo_l', allowed_modules=['detection'])"

COPY --chown=user . .

EXPOSE 7860
# Faqat 1 ta worker: galereya va nonce keshi jarayon xotirasida
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
