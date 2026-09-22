"""Yuzni aniqlash va embedding olish.

Asosiy dvigatel: InsightFace (SCRFD detektor + ArcFace r100 "buffalo_l").
ArcFace millionlab yuzlarda (turli yosh, makiyaj, yoritish) o'qitilgan, shuning uchun
yosh o'zgarishi va makiyajga nisbatan chidamli. Qo'shimcha chidamlilik matcher.py dagi
ko'p shablonli (multi-template) va moslashuvchan yangilash mexanizmi bilan beriladi.

Ko'z qisish / og'iz ochish kabi harakatlar uchun MediaPipe FaceLandmarker (478 nuqta) ishlatiladi.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

FACE_LANDMARKER_MODEL = os.environ.get("FACEPAY_FACE_LANDMARKER", "models/face_landmarker.task")


@dataclass
class Face:
    bbox: np.ndarray                 # [x1, y1, x2, y2]
    det_score: float
    embedding: np.ndarray            # 512 o'lchamli, normallashtirilgan
    yaw: float = 0.0                 # gradus: bosh chapga/o'ngga
    pitch: float = 0.0               # gradus: bosh yuqoriga/pastga
    roll: float = 0.0
    ear: float | None = None         # Eye Aspect Ratio — ko'z ochiqligi
    mar: float | None = None         # Mouth Aspect Ratio — og'iz ochiqligi
    extra: dict = field(default_factory=dict)

    @property
    def size(self) -> float:
        return float(min(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]))

    @property
    def area(self) -> float:
        return float((self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1]))


class FaceAnalyzer(Protocol):
    def analyze(self, image_bgr: np.ndarray) -> list[Face]: ...


# MediaPipe FaceMesh indekslari (EAR/MAR formulasi, Soukupova & Cech, 2016)
_LEFT_EYE = [33, 160, 158, 133, 153, 144]
_RIGHT_EYE = [362, 385, 387, 263, 373, 380]
_MOUTH = [61, 81, 311, 291, 402, 178]  # chap burchak, yuqori, yuqori, o'ng burchak, past, past


def _aspect_ratio(pts: np.ndarray) -> float:
    # (|p2-p6| + |p3-p5|) / (2|p1-p4|)
    a = np.linalg.norm(pts[1] - pts[5])
    b = np.linalg.norm(pts[2] - pts[4])
    c = np.linalg.norm(pts[0] - pts[3])
    return float((a + b) / (2.0 * c + 1e-6))


class InsightFaceAnalyzer:
    def __init__(self, model_name: str = "buffalo_l", det_size: int = 640, use_mediapipe: bool = True):
        from insightface.app import FaceAnalysis  # og'ir import — faqat kerak bo'lganda

        self._app = FaceAnalysis(
            name=model_name,
            allowed_modules=["detection", "recognition", "landmark_3d_68"],
            providers=["CPUExecutionProvider"],
        )
        self._app.prepare(ctx_id=-1, det_size=(det_size, det_size))
        self._mesh = None
        if use_mediapipe and os.path.exists(FACE_LANDMARKER_MODEL):
            try:
                from mediapipe.tasks.python import BaseOptions, vision

                self._mesh = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
                    base_options=BaseOptions(model_asset_path=FACE_LANDMARKER_MODEL, delegate=BaseOptions.Delegate.CPU),
                    num_faces=3, output_face_blendshapes=True))
            except Exception as e:  # pragma: no cover
                log.warning("MediaPipe yuklanmadi, ko'z qisish tekshiruvi o'chirildi: %s", e)
        elif use_mediapipe:
            log.warning("%s topilmadi — ko'z qisish/og'iz ochish sinovlari o'chirildi", FACE_LANDMARKER_MODEL)

    @property
    def supports_eye_mouth(self) -> bool:
        return self._mesh is not None

    def analyze(self, image_bgr: np.ndarray) -> list[Face]:
        faces = []
        for f in self._app.get(image_bgr):
            emb = f.normed_embedding.astype(np.float32)
            pitch, yaw, roll = (f.pose if getattr(f, "pose", None) is not None else (0.0, 0.0, 0.0))
            faces.append(Face(bbox=f.bbox.astype(np.float32), det_score=float(f.det_score),
                              embedding=emb, yaw=float(yaw), pitch=float(pitch), roll=float(roll)))
        if self._mesh is not None and faces:
            self._attach_eye_mouth(image_bgr, faces)
        return faces

    def _attach_eye_mouth(self, image_bgr: np.ndarray, faces: list[Face]) -> None:
        import cv2
        import mediapipe as mp

        h, w = image_bgr.shape[:2]
        rgb = np.ascontiguousarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
        res = self._mesh.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        for k, lm in enumerate(res.face_landmarks):
            pts = np.array([[p.x * w, p.y * h] for p in lm], dtype=np.float32)
            cx, cy = pts[:, 0].mean(), pts[:, 1].mean()
            # FaceMesh natijasini markazi bbox ichiga tushgan InsightFace yuziga bog'laymiz
            for face in faces:
                x1, y1, x2, y2 = face.bbox
                if x1 <= cx <= x2 and y1 <= cy <= y2:
                    face.ear = (_aspect_ratio(pts[_LEFT_EYE]) + _aspect_ratio(pts[_RIGHT_EYE])) / 2
                    face.mar = _aspect_ratio(pts[_MOUTH])
                    if res.face_blendshapes:
                        face.extra["blendshapes"] = {c.category_name: c.score for c in res.face_blendshapes[k]}
                    break


_analyzer: FaceAnalyzer | None = None


def get_analyzer() -> FaceAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = InsightFaceAnalyzer()
    return _analyzer


def set_analyzer(analyzer: FaceAnalyzer) -> None:
    """Testlarda soxta (mock) analizatorni o'rnatish uchun."""
    global _analyzer
    _analyzer = analyzer
