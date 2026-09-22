from datetime import datetime, timedelta

import numpy as np

from app.biometrics.liveness import FrameObs, generate_challenge, verify_active
from app.biometrics.matcher import (Gallery, SearchResult, TemplateInfo, decide, needs_reenrollment,
                                    plan_template_update)
from app.security.template_protection import normalize
from tests.conftest import noisy, random_unit


# ---------------- Liveness ----------------

def frames_for(steps, ear_open=0.3):
    """Haqiqiy odam: to'g'ri qaraydi, keyin harakatlarni bajaradi."""
    seq = [(0, ear_open, 0.1)] * 3
    for s in steps:
        if s == "turn_left":
            seq += [(10, ear_open, 0.1), (25, ear_open, 0.1), (10, ear_open, 0.1), (0, ear_open, 0.1)]
        elif s == "turn_right":
            seq += [(-10, ear_open, 0.1), (-25, ear_open, 0.1), (-10, ear_open, 0.1), (0, ear_open, 0.1)]
        elif s == "blink":
            seq += [(0, ear_open, 0.1), (0, 0.1, 0.1), (0, ear_open, 0.1)]
        elif s == "open_mouth":
            seq += [(0, ear_open, 0.1), (0, ear_open, 0.6), (0, ear_open, 0.1)]
    seq += [(0, ear_open, 0.1)] * 2
    return [FrameObs(1000 + k * 150, y, e, m) for k, (y, e, m) in enumerate(seq)]


def test_active_liveness_passes_for_real_person():
    for steps in (["turn_left", "blink"], ["open_mouth", "turn_right"], ["turn_left", "turn_right"]):
        assert verify_active(steps, frames_for(steps)).passed, steps


def test_active_liveness_rejects_static_photo():
    photo = [FrameObs(1000 + k * 150, 0.0, 0.3, 0.1) for k in range(15)]
    r = verify_active(["turn_left", "blink"], photo)
    assert not r.passed


def test_active_liveness_rejects_wrong_order():
    """Oldindan yozilgan video: harakatlar bor, lekin boshqa tartibda."""
    r = verify_active(["blink", "turn_left"], frames_for(["turn_left", "blink"]))
    assert not r.passed


def test_active_liveness_rejects_wrong_direction():
    assert not verify_active(["turn_left"], frames_for(["turn_right"])).passed


def test_active_liveness_rejects_bad_timestamps():
    f = frames_for(["turn_left"])
    f[3].ts_ms = f[2].ts_ms  # vaqt oldinga yurmayapti
    assert not verify_active(["turn_left"], f).passed
    too_fast = [FrameObs(1000 + k, fr.yaw, fr.ear, fr.mar) for k, fr in enumerate(frames_for(["turn_left"]))]
    assert not verify_active(["turn_left"], too_fast).passed


def test_blink_requires_close_and_reopen():
    seq = frames_for(["turn_left"])
    # ko'z yumildi, lekin ochilmadi (masalan, ko'zi yumuq rasm bilan almashtirildi)
    seq += [FrameObs(seq[-1].ts_ms + 150 * (k + 1), 0, 0.1, 0.1) for k in range(4)]
    assert not verify_active(["turn_left", "blink"], seq).passed


def test_challenge_is_random_and_contains_head_action():
    seen = set()
    for _ in range(50):
        c = generate_challenge(2, True)
        assert len(c) == 2 and len(set(c)) == 2
        assert any(a in ("turn_left", "turn_right") for a in c)
        seen.add(tuple(c))
    assert len(seen) > 3
    assert all(a in ("turn_left", "turn_right") for a in generate_challenge(2, False))


# ---------------- 1:N qidiruv va qaror ----------------

def test_gallery_identifies_correct_person(rng):
    g = Gallery(512)
    people = {f"u{k}": random_unit(rng) for k in range(200)}
    for uid, v in people.items():
        g.upsert(uid, f"t-{uid}", v)
    probe = noisy(people["u17"], rng, 0.7)
    r = g.search(probe)
    assert r.user_id == "u17"
    assert decide(r).accepted


def test_unknown_person_rejected(rng):
    g = Gallery(512)
    for k in range(200):
        g.upsert(f"u{k}", f"t{k}", random_unit(rng))
    r = g.search(random_unit(rng))
    assert not decide(r).accepted


def test_lookalikes_rejected_by_margin():
    """Egizaklar yoki bir-biriga juda o'xshash odamlar: kim to'layotgani noaniq -> rad."""
    r = SearchResult("u1", 0.62, "t1", 0.58)
    d = decide(r)
    assert not d.accepted and d.reason == "noaniq_moslik"


def test_borderline_score_requires_pin():
    assert decide(SearchResult("u1", 0.48, "t1", 0.05)).borderline
    assert not decide(SearchResult("u1", 0.75, "t1", 0.05)).borderline


# ---------------- Yosh o'zgarishi ----------------

def aging_face(e0, d, year, deg_per_year=7.0):
    th = np.radians(deg_per_year * year)
    return normalize(np.cos(th) * e0 + np.sin(th) * d)


def test_aging_template_adaptation(rng):
    """10 yil davomida yuz asta-sekin o'zgaradi (har yili ~7 gradus).

    Moslashuvsiz: 10 yildan keyin eski shablon bilan moslik chegaradan past (tanilmaydi).
    Moslashuv bilan: shablon odam bilan birga "qariydi" va tanib olish davom etadi.
    """
    e0 = random_unit(rng)
    d = random_unit(rng)
    d = normalize(d - (d @ e0) * e0)

    static = e0.copy()
    now = datetime(2026, 1, 1)
    templates = [TemplateInfo("t0", e0.copy(), "enroll", None, now - timedelta(days=2))]

    for year in range(1, 11):
        for visit in range(6):  # yiliga bir necha marta to'lov
            now += timedelta(days=60)
            probe = noisy(aging_face(e0, d, year), rng, 0.93)
            score = max(float(t.vec @ probe) for t in templates)
            plan = plan_template_update(templates, probe, score, now)
            if plan.action in ("ema", "replace"):
                t = next(t for t in templates if t.id == plan.template_id)
                t.vec, t.updated_at = plan.new_vec, now
            elif plan.action == "add":
                templates.append(TemplateInfo(f"t{len(templates)}", plan.new_vec, "adaptive", None, now))

    final_probe = noisy(aging_face(e0, d, 10), rng, 0.93)
    static_score = float(static @ final_probe)
    adaptive_score = max(float(t.vec @ final_probe) for t in templates)
    assert static_score < 0.45, static_score
    assert adaptive_score > 0.60, adaptive_score


def test_template_update_rate_limited(rng):
    """Kuniga bir martadan ko'p yangilanmaydi — shablonni 'zaharlash' hujumi sekinlashadi."""
    now = datetime(2026, 1, 1)
    e = random_unit(rng)
    t = [TemplateInfo("t0", e, "enroll", None, now - timedelta(hours=2))]
    assert plan_template_update(t, noisy(e, rng, 0.9), 0.9, now).action == "none"


def test_low_confidence_never_updates(rng):
    now = datetime(2026, 1, 1)
    e = random_unit(rng)
    t = [TemplateInfo("t0", e, "enroll", None, now - timedelta(days=5))]
    assert plan_template_update(t, noisy(e, rng, 0.5), 0.5, now).action == "none"


def test_new_look_added_as_separate_template(rng):
    """Makiyaj / soqol: o'xshashlik 0.60-0.80 — alohida shablon qo'shiladi."""
    now = datetime(2026, 1, 1)
    e = random_unit(rng)
    t = [TemplateInfo("t0", e, "enroll", None, now - timedelta(days=5))]
    look = noisy(e, rng, 0.68)
    assert plan_template_update(t, look, 0.68, now).action == "add"


def test_reenrollment_recommendation():
    now = datetime(2030, 1, 1)
    assert needs_reenrollment([], now - timedelta(days=4 * 365), now)
    assert needs_reenrollment([0.48] * 15, now - timedelta(days=100), now)
    assert not needs_reenrollment([0.7] * 15, now - timedelta(days=100), now)


def test_turn_measured_relative_to_starting_pose():
    """Kamera yon tomonda (masalan, boshlang'ich yaw = -15): burilish shunga nisbatan o'lchanadi."""
    shifted = [FrameObs(f.ts_ms, f.yaw - 15, f.ear, f.mar) for f in frames_for(["turn_left", "turn_right"])]
    assert verify_active(["turn_left", "turn_right"], shifted).passed


def test_blink_detected_from_blendshapes():
    """EAR o'zgarmasa ham (past kadr sifati) MediaPipe eyeBlink ko'rsatkichi bo'yicha aniqlanadi."""
    seq = [FrameObs(1000 + k * 130, 0, 0.3, 0.1, blink=b, jaw=0.05)
           for k, b in enumerate([0.1, 0.1, 0.1, 0.1, 0.7, 0.1, 0.1, 0.1])]
    assert verify_active(["blink"], seq).passed
    no_blink = [FrameObs(1000 + k * 130, 0, 0.3, 0.1, blink=0.1, jaw=0.05) for k in range(8)]
    assert not verify_active(["blink"], no_blink).passed


def test_failed_liveness_reports_stats():
    r = verify_active(["turn_left"], [FrameObs(1000 + k * 150, 3.0, 0.3, 0.1) for k in range(10)])
    assert not r.passed and r.stats["rel_yaw_max"] == 0.0 and r.stats["n"] == 10
