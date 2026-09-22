"""Yuzni solishtirish (1:N qidiruv), yosh o'zgarishi va makiyajga moslashish.

1:N — to'lovchi o'zini tanishtirmaydi (karta/telefon yo'q), shuning uchun yuz butun
bazadan qidiriladi. Xato to'lov (boshqa odam hisobidan yechish) eng xavfli xato,
shuning uchun uchta shart bor:
  1) eng yaxshi ball >= match_threshold
  2) 1-o'rin va 2-o'rin (boshqa foydalanuvchi) orasidagi farq >= min_margin
     (egizaklar, juda o'xshash odamlar, og'ir makiyaj bilan boshqaga o'xshatish)
  3) katta summa yoki "chegaradagi" ball -> PIN talab qilinadi (payment.py)

YOSH O'ZGARISHI:
  - Har foydalanuvchida bir nechta shablon (max 6). Ball = shablonlar ichida eng yuqorisi.
  - Yuqori ishonchli muvaffaqiyatli to'lovdan keyin shablon asta-sekin yangilanadi (EMA),
    shunda yillar davomida yuz o'zgarsa ham shablon u bilan "birga qariydi".
  - Ballar tarixi kuzatiladi: o'rtacha ball pasayib borsa -> qayta ro'yxatdan o'tish taklifi.

MAKIYAJ / KO'ZOYNAK / SOQOL:
  - Ro'yxatdan o'tishda turli burchakdagi kadrlardan bir nechta shablon olinadi.
  - Foydalanuvchi qo'shimcha "variant" shablon qo'shishi mumkin (masalan, bayramona makiyaj).
  - Ishonchli moslikda yangi ko'rinish (o'xshashlik 0.60-0.80) alohida "adaptive"
    shablon sifatida qo'shiladi.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from app.config import get_settings
from app.security.template_protection import normalize


@dataclass
class SearchResult:
    user_id: str | None
    score: float
    template_id: str | None
    second_score: float  # eng yaqin BOSHQA foydalanuvchi


@dataclass
class Decision:
    accepted: bool
    reason: str | None
    borderline: bool  # ball chegaraga yaqin -> PIN talab qilinadi


def decide(result: SearchResult) -> Decision:
    s = get_settings()
    if result.user_id is None or result.score < s.match_threshold:
        return Decision(False, "yuz_tanilmadi", False)
    if result.score - result.second_score < s.min_margin:
        return Decision(False, "noaniq_moslik", False)
    borderline = result.score < (s.match_threshold + s.high_confidence_threshold) / 2
    return Decision(True, None, borderline)


class Gallery:
    """Shablonlar xotirada (RAM) deshifrlangan holda. Diskka yozilmaydi.

    Katta bazalar uchun (millionlab) FAISS/HNSW indeks bilan almashtiriladi.
    """

    def __init__(self, dim: int):
        self._dim = dim
        self._lock = threading.RLock()
        self._matrix = np.zeros((0, dim), dtype=np.float32)
        self._owners: list[str] = []
        self._template_ids: list[str] = []

    def replace_all(self, rows: list[tuple[str, str, np.ndarray]]) -> None:
        with self._lock:
            self._owners = [r[0] for r in rows]
            self._template_ids = [r[1] for r in rows]
            self._matrix = (np.stack([normalize(r[2]) for r in rows]) if rows
                            else np.zeros((0, self._dim), dtype=np.float32))

    def upsert(self, user_id: str, template_id: str, vec: np.ndarray) -> None:
        with self._lock:
            vec = normalize(vec)
            if template_id in self._template_ids:
                self._matrix[self._template_ids.index(template_id)] = vec
            else:
                self._matrix = np.vstack([self._matrix, vec[None]])
                self._owners.append(user_id)
                self._template_ids.append(template_id)

    def remove_template(self, template_id: str) -> None:
        with self._lock:
            if template_id in self._template_ids:
                k = self._template_ids.index(template_id)
                self._matrix = np.delete(self._matrix, k, axis=0)
                del self._owners[k]
                del self._template_ids[k]

    def remove_user(self, user_id: str) -> None:
        with self._lock:
            keep = [k for k, o in enumerate(self._owners) if o != user_id]
            self._matrix = self._matrix[keep]
            self._owners = [self._owners[k] for k in keep]
            self._template_ids = [self._template_ids[k] for k in keep]

    def __len__(self) -> int:
        return len(self._owners)

    def search(self, probe: np.ndarray, exclude_user: str | None = None) -> SearchResult:
        with self._lock:
            if len(self._owners) == 0:
                return SearchResult(None, 0.0, None, 0.0)
            sims = self._matrix @ normalize(probe)
            best_per_user: dict[str, tuple[float, str]] = {}
            for k, sim in enumerate(sims):
                owner = self._owners[k]
                if owner == exclude_user:
                    continue
                if owner not in best_per_user or sim > best_per_user[owner][0]:
                    best_per_user[owner] = (float(sim), self._template_ids[k])
        if not best_per_user:
            return SearchResult(None, 0.0, None, 0.0)
        ranked = sorted(best_per_user.items(), key=lambda kv: kv[1][0], reverse=True)
        top_user, (top_score, top_tid) = ranked[0]
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0
        return SearchResult(top_user, top_score, top_tid, second)


# ---------------- Moslashuvchan shablon yangilash ----------------

@dataclass
class TemplateInfo:
    id: str
    vec: np.ndarray
    source: str
    last_matched_at: datetime | None
    updated_at: datetime


@dataclass
class UpdatePlan:
    action: str               # "ema" | "add" | "replace" | "none"
    template_id: str | None = None
    new_vec: np.ndarray | None = None


def plan_template_update(templates: list[TemplateInfo], probe: np.ndarray, score: float,
                         now: datetime) -> UpdatePlan:
    """Faqat yuqori ishonchli, tirikligi tasdiqlangan, sifatli kadrdan keyin chaqiriladi."""
    s = get_settings()
    if score < s.high_confidence_threshold or not templates:
        return UpdatePlan("none")
    # "Zaharlash" (template poisoning) hujumini sekinlashtirish: kuniga ko'pi bilan 1 yangilanish
    if max(t.updated_at for t in templates) > now - timedelta(hours=24):
        return UpdatePlan("none")

    probe = normalize(probe)
    sims = [float(t.vec @ probe) for t in templates]
    best = int(np.argmax(sims))

    if sims[best] >= s.template_diversity_threshold:
        # Tanish ko'rinish: eng yaqin shablonni asta-sekin siljitamiz (yosh o'zgarishi)
        a = s.template_ema_alpha
        return UpdatePlan("ema", templates[best].id, normalize((1 - a) * templates[best].vec + a * probe))

    # Yangi ko'rinish (makiyaj, soqol, ko'zoynak, yillar o'tishi) — alohida shablon
    if len(templates) < s.max_templates_per_user:
        return UpdatePlan("add", None, probe)
    adaptive = [t for t in templates if t.source == "adaptive"]
    if adaptive:
        oldest = min(adaptive, key=lambda t: t.last_matched_at or t.updated_at)
        return UpdatePlan("replace", oldest.id, probe)
    a = s.template_ema_alpha
    return UpdatePlan("ema", templates[best].id, normalize((1 - a) * templates[best].vec + a * probe))


def needs_reenrollment(recent_scores: list[float], last_enrolled_at: datetime, now: datetime) -> bool:
    s = get_settings()
    if now - last_enrolled_at > timedelta(days=s.reenroll_after_days):
        return True
    if len(recent_scores) >= 10 and float(np.mean(recent_scores)) < s.match_threshold + 0.07:
        return True
    return False
