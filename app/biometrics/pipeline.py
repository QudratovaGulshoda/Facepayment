"""Kadrlardan bitta ishonchli, tirik, himoyalangan embedding olish.

Qadamlar:
 1. Har bir kadrni dekodlash (xotirada, diskka yozilmaydi)
 2. Asosiy yuzni tanlash (bir nechta yuz -> rad)
 3. Faol liveness: harakatlar ketma-ketligi
 4. Shaxs izchilligi: barcha kadrlar bir odamga tegishli
 5. Passiv liveness: old tomondan qaralgan kadrlar bo'yicha
 6. Sifat: eng sifatli old kadrlar tanlanadi va o'rtachalanadi
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.biometrics.analyzer import Face, get_analyzer
from app.biometrics.liveness import FrameObs, PassiveLiveness, verify_active
from app.biometrics.quality import assess, select_primary_face
from app.config import get_settings
from app.security.template_protection import normalize

log = logging.getLogger("facepay.pipeline")

MAX_FRAME_BYTES = 400_000
MAX_FRAMES = 60
# Bir odamning turli burchakdagi kadrlari orasida ArcFace o'xshashligi odatda 0.5-0.9,
# turli odamlar orasida 0-0.25. Har bir JUFT kadr tekshiriladi.
IDENTITY_CONSISTENCY_MIN = 0.35

_passive: PassiveLiveness | None = None


def get_passive() -> PassiveLiveness:
    global _passive
    if _passive is None:
        _passive = PassiveLiveness()
    return _passive


def set_passive(p: PassiveLiveness) -> None:
    global _passive
    _passive = p


class BiometricError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class CaptureResult:
    embedding: np.ndarray                 # asosiy (o'rtachalangan) embedding
    extra_embeddings: list[np.ndarray]    # ro'yxatdan o'tish uchun turli burchaklar
    quality: float
    passive_score: float
    reasons: list[str] = field(default_factory=list)


def decode_frame(b64: str) -> np.ndarray:
    raw = base64.b64decode(b64, validate=True)
    if len(raw) > MAX_FRAME_BYTES:
        raise BiometricError("kadr_juda_katta")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise BiometricError("kadr_notogri")
    return img


def process_capture(frames_b64: list[str], timestamps_ms: list[int], challenge_steps: list[str]) -> CaptureResult:
    s = get_settings()
    if len(frames_b64) != len(timestamps_ms):
        raise BiometricError("kadrlar_soni_mos_emas")
    if not (s.min_frames <= len(frames_b64) <= MAX_FRAMES):
        raise BiometricError("kadrlar_soni_notogri")

    analyzer = get_analyzer()
    passive = get_passive()

    images: list[np.ndarray] = []
    faces: list[Face] = []
    for b64 in frames_b64:
        img = decode_frame(b64)
        face, err = select_primary_face(analyzer.analyze(img), img.shape)
        if err:
            raise BiometricError(err)
        images.append(img)
        faces.append(face)

    # 3. Faol liveness
    obs = []
    for ts, f in zip(timestamps_ms, faces):
        bs = f.extra.get("blendshapes") or {}
        blink = max(bs.get("eyeBlinkLeft", 0.0), bs.get("eyeBlinkRight", 0.0)) if bs else None
        obs.append(FrameObs(ts, f.yaw, f.ear, f.mar, blink, bs.get("jawOpen") if bs else None))
    active = verify_active(challenge_steps, obs)
    if not active.passed:
        # Diagnostika: faqat sonlar (burilish burchagi, ko'z/og'iz ko'rsatkichlari) — shaxsiy ma'lumot yo'q
        log.warning("liveness_faol rad: steps=%s reason=%s stats=%s", challenge_steps, active.reason, active.stats)
        raise BiometricError(f"liveness_faol:{active.reason}")

    # 4. Shaxs izchilligi: HAR BIR juft kadr bir odamga tegishli bo'lishi kerak.
    # (O'rtacha vektor bilan solishtirish yetarli emas: yarmi hujumchi, yarmi qurbon bo'lsa,
    # o'rtacha vektor ikkalasiga ham ~0.7 o'xshash bo'lib qoladi.)
    embs = np.stack([normalize(f.embedding) for f in faces])
    if float((embs @ embs.T).min()) < IDENTITY_CONSISTENCY_MIN:
        raise BiometricError("kadrlarda_turli_shaxs")

    # 5-6. Old tomondan qaralgan kadrlar: sifat + passiv liveness
    frontal = [k for k, f in enumerate(faces) if abs(f.yaw) <= s.max_yaw_deg and abs(f.pitch) <= s.max_pitch_deg]
    if len(frontal) < 2:
        raise BiometricError("old_kadrlar_kam")
    qualities = {k: assess(images[k], faces[k]) for k in frontal}
    good = [k for k in frontal if qualities[k].ok]
    if len(good) < 2:
        reasons = sorted({r for q in qualities.values() for r in q.reasons})
        raise BiometricError("sifat_past:" + ",".join(reasons))

    passive_scores = [passive.score(images[k], faces[k]) for k in good]
    # Median: bitta tasodifiy yaxshi kadr hujumchiga yordam bermasligi uchun
    passive_score = float(np.median(passive_scores))
    threshold = s.passive_liveness_threshold if passive.has_model else 0.45
    if passive_score < threshold:
        raise BiometricError("liveness_passiv")

    best = sorted(good, key=lambda k: qualities[k].score, reverse=True)[:5]
    embedding = normalize(np.mean([embs[k] for k in best], axis=0))

    # Ro'yxatdan o'tish uchun: turli burchaklardagi (burilgan) kadrlar ham foydali,
    # ular profil holatda tanib olishni yaxshilaydi
    turned = [k for k in range(len(faces)) if k not in frontal and faces[k].det_score > 0.7]
    extra = [embs[k] for k in turned[:2]]

    return CaptureResult(
        embedding=embedding,
        extra_embeddings=extra,
        quality=float(np.mean([qualities[k].score for k in best])),
        passive_score=passive_score,
    )
