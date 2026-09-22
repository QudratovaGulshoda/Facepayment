"""Kadr sifatini baholash.

Yomon kadrdan olingan embedding noto'g'ri moslikka olib keladi, shuning uchun
solishtirishdan OLDIN sifat tekshiriladi: yuz o'lchami, aniqlik (xiralik), yorug'lik,
bosh burilishi, detektor ishonchi.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.biometrics.analyzer import Face
from app.config import get_settings


@dataclass
class QualityResult:
    ok: bool
    score: float  # 0..1
    reasons: list[str] = field(default_factory=list)


def crop_face(image_bgr: np.ndarray, face: Face, scale: float = 1.0) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    x1, y1, x2, y2 = face.bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = max(x2 - x1, y2 - y1) * scale / 2
    xa, ya = int(max(0, cx - half)), int(max(0, cy - half))
    xb, yb = int(min(w, cx + half)), int(min(h, cy + half))
    return image_bgr[ya:yb, xa:xb]


def assess(image_bgr: np.ndarray, face: Face, frontal_required: bool = True) -> QualityResult:
    s = get_settings()
    reasons: list[str] = []
    crop = crop_face(image_bgr, face)
    if crop.size == 0:
        return QualityResult(False, 0.0, ["yuz_kadrdan_tashqarida"])
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())

    if face.size < s.min_face_size_px:
        reasons.append("yuz_juda_kichik")
    if sharpness < s.min_sharpness:
        reasons.append("xira_kadr")
    if brightness < s.min_brightness:
        reasons.append("juda_qorongi")
    if brightness > s.max_brightness:
        reasons.append("juda_yorug")
    if face.det_score < 0.6:
        reasons.append("yuz_ishonchsiz")  # ko'pincha niqob, qo'l bilan to'silgan yuz
    if frontal_required and (abs(face.yaw) > s.max_yaw_deg or abs(face.pitch) > s.max_pitch_deg):
        reasons.append("bosh_burilgan")

    # Umumiy ball: har bir omilning me'yorlashtirilgan hissasi
    score = float(np.mean([
        min(1.0, face.size / (2 * s.min_face_size_px)),
        min(1.0, sharpness / (3 * s.min_sharpness)),
        1.0 - min(1.0, abs(brightness - 128) / 128),
        1.0 - min(1.0, (abs(face.yaw) + abs(face.pitch)) / 90),
        face.det_score,
    ]))
    return QualityResult(ok=not reasons, score=score, reasons=reasons)


def select_primary_face(faces: list[Face], image_shape: tuple) -> tuple[Face | None, str | None]:
    """Metroda orqada boshqa odamlar turishi mumkin. To'lovchi — eng katta va markazdagi yuz.
    Agar ikkinchi yuz ham deyarli shunday katta bo'lsa, kim to'layotgani noaniq -> rad etamiz.
    """
    if not faces:
        return None, "yuz_topilmadi"
    faces = sorted(faces, key=lambda f: f.area, reverse=True)
    primary = faces[0]
    if len(faces) > 1 and faces[1].area > 0.5 * primary.area:
        return None, "bir_nechta_yuz"
    h, w = image_shape[:2]
    cx = (primary.bbox[0] + primary.bbox[2]) / 2
    if abs(cx - w / 2) > w * 0.3:
        return None, "yuz_markazda_emas"
    return primary, None
