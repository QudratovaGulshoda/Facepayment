"""Tirik odam ekanligini tekshirish (Presentation Attack Detection).

Hujum turlari va himoya:
 | Hujum                          | Himoya                                                 |
 |--------------------------------|--------------------------------------------------------|
 | Chop etilgan foto              | Faol sinov (ko'z qisish, bosh burish) + passiv model   |
 | Telefon/planshetdagi video     | Tasodifiy sinov ketma-ketligi + ekran muare tahlili    |
 | Oldindan yozilgan video        | Sinov serverda tasodifiy tanlanadi, 30 s amal qiladi   |
 | Kadrlar orasida yuzni almashish| Barcha kadrlarda bir xil shaxs ekanligi tekshiriladi   |
 | Kamera oqimini soxtalashtirish | Terminal Ed25519 imzosi (terminal_auth.py)             |
 | 3D niqob                       | Passiv model + (prod'da) IQ/chuqurlik kamerasi tavsiya |

1) FAOL (active) — server tasodifiy harakatlar ketma-ketligini beradi, terminal kadrlarni
   yuboradi, server harakatlar TO'G'RI TARTIBDA bajarilganini tekshiradi.
2) PASSIV (passive) — har bir kadr alohida baholanadi (MiniFASNet ONNX modeli bo'lsa u,
   bo'lmasa zaif evristika: ekran muaresi va rang diapazoni).
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field

import cv2
import numpy as np

from app.biometrics.analyzer import Face
from app.biometrics.quality import crop_face

log = logging.getLogger(__name__)

HEAD_ACTIONS = ["turn_left", "turn_right"]
EYE_MOUTH_ACTIONS = ["blink", "open_mouth"]

ACTION_TEXT_UZ = {
    "blink": "Ko'zingizni qising",
    "turn_left": "Boshingizni chapga buring",
    "turn_right": "Boshingizni o'ngga buring",
    "open_mouth": "Og'zingizni oching",
}

TURN_DEG = 18.0       # burilish deb hisoblash uchun minimal yaw
FRONTAL_DEG = 12.0
BLINK_CLOSED_RATIO = 0.65
BLINK_OPEN_RATIO = 0.85
MOUTH_OPEN_MAR = 0.45
# Kalibrlash (InsightFace buffalo_l, xom kamera kadri — ko'zgu EMAS): odam O'Z chap tomoniga
# burilganda burun rasmning o'ng tomoniga siljiydi va yaw MUSBAT bo'ladi.
# Terminal kadrni ko'zgu qilib yuborsa, FACEPAY_YAW_SIGN=-1.
YAW_SIGN = float(os.environ.get("FACEPAY_YAW_SIGN", "1"))


def generate_challenge(n_steps: int, eye_mouth_supported: bool) -> list[str]:
    """Kriptografik tasodifiy (secrets) ketma-ketlik. Kamida bitta bosh harakati bo'ladi."""
    rng = secrets.SystemRandom()
    head = rng.choice(HEAD_ACTIONS)
    if eye_mouth_supported:
        steps = [head] + rng.sample(EYE_MOUTH_ACTIONS, min(max(n_steps - 1, 0), len(EYE_MOUTH_ACTIONS)))
        rng.shuffle(steps)
        return steps
    # Faqat bosh harakatlari mavjud: chap/o'ng navbatma-navbat
    other = "turn_right" if head == "turn_left" else "turn_left"
    return [head if k % 2 == 0 else other for k in range(max(n_steps, 1))]


@dataclass
class FrameObs:
    ts_ms: int
    yaw: float
    ear: float | None
    mar: float | None
    blink: float | None = None   # MediaPipe blendshape: max(eyeBlinkLeft, eyeBlinkRight), 0..1
    jaw: float | None = None     # MediaPipe blendshape: jawOpen, 0..1


@dataclass
class ActiveResult:
    passed: bool
    completed: list[str] = field(default_factory=list)
    reason: str | None = None
    stats: dict = field(default_factory=dict)  # diagnostika uchun (faqat sonlar, shaxsiy ma'lumot yo'q)


BLINK_BS_CLOSED = 0.45
BLINK_BS_OPEN = 0.30
JAW_OPEN_BS = 0.35


def _is_closed(f: FrameObs, ear_open: float | None) -> bool:
    if f.blink is not None and f.blink > BLINK_BS_CLOSED:
        return True
    return f.ear is not None and ear_open is not None and f.ear < BLINK_CLOSED_RATIO * ear_open


def _is_open(f: FrameObs, ear_open: float | None) -> bool:
    if f.blink is not None:
        return f.blink < BLINK_BS_OPEN
    return f.ear is not None and ear_open is not None and f.ear > BLINK_OPEN_RATIO * ear_open


def _stats(frames: list[FrameObs], base_yaw: float) -> dict:
    rel = [(f.yaw - base_yaw) * YAW_SIGN for f in frames]
    pick = lambda xs: [round(x, 3) for x in xs if x is not None]  # noqa: E731
    ears, mars, blinks, jaws = (pick([getattr(f, a) for f in frames]) for a in ("ear", "mar", "blink", "jaw"))
    return {
        "n": len(frames), "base_yaw": round(base_yaw, 1),
        "rel_yaw_min": round(min(rel), 1), "rel_yaw_max": round(max(rel), 1),
        "ear_min": min(ears, default=None), "ear_max": max(ears, default=None),
        "mar_max": max(mars, default=None), "blink_max": max(blinks, default=None),
        "jaw_max": max(jaws, default=None),
    }


def verify_active(steps: list[str], frames: list[FrameObs]) -> ActiveResult:
    if len(frames) < 4:
        return ActiveResult(False, reason="kadrlar_kam")
    ts = [f.ts_ms for f in frames]
    if any(b <= a for a, b in zip(ts, ts[1:])):
        return ActiveResult(False, reason="vaqt_tamgasi_notogri")
    duration = ts[-1] - ts[0]
    if duration < 800 or duration > 30_000:
        return ActiveResult(False, reason="davomiylik_notogri")

    # Boshlang'ich holat: kamera har doim ham yuzning ro'parasida bo'lmaydi (noutbuk, turniket
    # balandligi). Burilish shu holatga NISBATAN o'lchanadi.
    base_yaw = float(np.median([f.yaw for f in frames[:3]]))
    stats = _stats(frames, base_yaw)
    if abs(base_yaw) > 25:
        return ActiveResult(False, reason="boshida_togri_qaramadi", stats=stats)

    ears = [f.ear for f in frames if f.ear is not None]
    ear_open = float(np.percentile(ears, 80)) if ears else None

    completed: list[str] = []
    i = 0
    for step in steps:
        found = False
        eye_closed_seen = False
        while i < len(frames):
            f = frames[i]
            i += 1
            yaw = (f.yaw - base_yaw) * YAW_SIGN
            if step == "turn_left" and yaw > TURN_DEG:
                found = True
            elif step == "turn_right" and yaw < -TURN_DEG:
                found = True
            elif step == "open_mouth" and ((f.jaw is not None and f.jaw > JAW_OPEN_BS)
                                           or (f.mar is not None and f.mar > MOUTH_OPEN_MAR)):
                found = True
            elif step == "blink":
                if _is_closed(f, ear_open):
                    eye_closed_seen = True
                elif eye_closed_seen and _is_open(f, ear_open):
                    found = True  # yopildi va qayta ochildi — haqiqiy ko'z qisish
            if found:
                break
        if not found:
            return ActiveResult(False, completed, reason=f"bajarilmadi:{step}", stats=stats)
        completed.append(step)
        # Keyingi harakatdan oldin boshlang'ich holatga qaytishini kutamiz (bosh harakatlari uchun)
        if step in HEAD_ACTIONS:
            while i < len(frames) and abs(frames[i].yaw - base_yaw) > FRONTAL_DEG:
                i += 1
    return ActiveResult(True, completed, stats=stats)


# ---------------- Passiv liveness ----------------

class PassiveLiveness:
    """MiniFASNetV2 (Silent-Face-Anti-Spoofing, Minivision, Apache-2.0) ONNX modeli.

    Model fayli: models/anti_spoof.onnx
      https://github.com/yakhyo/face-anti-spoofing/releases/download/weights/MiniFASNetV2.onnx
    Preprocessing asl implementatsiyaga mos: bbox 2.7 marta kengaytiriladi, BGR, 0..255
    (normallashtirishsiz). Chiqish: 3 sinf logitlari, 'real' indeksi = 1.
    """

    def __init__(self, model_path: str = "models/anti_spoof.onnx", crop_scale: float = 2.7):
        self._session = None
        self._scale = crop_scale
        if os.path.exists(model_path):
            import onnxruntime as ort

            self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            inp = self._session.get_inputs()[0]
            self._input = inp.name
            self._hw = tuple(int(d) for d in inp.shape[2:])
            log.info("Passiv liveness modeli yuklandi: %s", model_path)
        else:
            log.warning("Passiv liveness modeli topilmadi (%s) — zaif evristika ishlatiladi", model_path)

    @property
    def has_model(self) -> bool:
        return self._session is not None

    def _crop(self, image_bgr: np.ndarray, face: Face) -> np.ndarray:
        src_h, src_w = image_bgr.shape[:2]
        x1, y1, x2, y2 = face.bbox
        bw, bh = x2 - x1, y2 - y1
        scale = min((src_h - 1) / bh, (src_w - 1) / bw, self._scale)
        cx, cy = x1 + bw / 2, y1 + bh / 2
        xa, ya = max(0, int(cx - bw * scale / 2)), max(0, int(cy - bh * scale / 2))
        xb, yb = min(src_w - 1, int(cx + bw * scale / 2)), min(src_h - 1, int(cy + bh * scale / 2))
        return cv2.resize(image_bgr[ya:yb + 1, xa:xb + 1], (self._hw[1], self._hw[0]))

    def score(self, image_bgr: np.ndarray, face: Face) -> float:
        if self._session is None:
            return heuristic_score(image_bgr, face)
        x = np.transpose(self._crop(image_bgr, face).astype(np.float32), (2, 0, 1))[None]
        logits = self._session.run(None, {self._input: x})[0][0]
        probs = np.exp(logits - logits.max())
        probs /= probs.sum()
        return float(probs[1])


def heuristic_score(image_bgr: np.ndarray, face: Face) -> float:
    """Zaif signal (model yo'q holat uchun). Faqat faol sinov bilan BIRGA ishlatiladi.

    - Ekran muaresi: ekrandan qayta suratga olinganda FFT spektrining yuqori chastotalarida
      davriy cho'qqilar paydo bo'ladi.
    - Rang diapazoni: chop etilgan/ekrandagi rasmlarda to'yinganlik va yorqinlik
      diapazoni odatda torayadi.
    """
    crop = crop_face(image_bgr, face, scale=1.2)
    if crop.size == 0:
        return 0.0
    crop = cv2.resize(crop, (128, 128))
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32)

    spec = np.abs(np.fft.fftshift(np.fft.fft2(gray - gray.mean())))
    yy, xx = np.mgrid[-64:64, -64:64]
    r = np.sqrt(xx**2 + yy**2)
    high = spec[(r > 30) & (r < 62)]
    peak_ratio = float(high.max() / (high.mean() + 1e-6))
    moire_score = float(np.clip(1.0 - (peak_ratio - 8.0) / 20.0, 0.0, 1.0))

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).astype(np.float32)
    sat_std, val_std = float(hsv[..., 1].std()), float(hsv[..., 2].std())
    color_score = float(np.clip((sat_std / 25.0 + val_std / 45.0) / 2.0, 0.0, 1.0))

    return 0.6 * moire_score + 0.4 * color_score
