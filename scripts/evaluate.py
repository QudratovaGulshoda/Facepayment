"""Tanib olish aniqligini baholash: FAR, FRR, EER va 1:N (margin qoidasi bilan).

Diplom ishining "Tajriba natijalari" bo'limi uchun. Ma'lumotlar to'plami tuzilishi:
    dataset/
      shaxs_1/ rasm1.jpg rasm2.jpg ...
      shaxs_2/ ...

Tavsiya etiladigan ochiq to'plamlar:
  - LFW           — umumiy tekshiruv
  - FG-NET, AgeDB — yosh o'zgarishi (bir odamning turli yoshdagi suratlari)
  - YMU / VMU     — makiyajli va makiyajsiz suratlar

    python scripts/evaluate.py path/to/dataset --out natijalar.csv
"""
import argparse
import csv
import itertools
import random
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.biometrics.analyzer import InsightFaceAnalyzer  # noqa: E402


def embed_dataset(root: Path, max_per_person: int) -> dict[str, list[np.ndarray]]:
    analyzer = InsightFaceAnalyzer(use_mediapipe=False, with_pose=False)  # bu yerda faqat embedding kerak
    out: dict[str, list[np.ndarray]] = {}
    people = sorted(p for p in root.iterdir() if p.is_dir())
    done = 0
    for n, person in enumerate(people, 1):
        embs = []
        for img_path in sorted(person.glob("*"))[:max_per_person]:
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            faces = analyzer.analyze(img)
            if faces:
                embs.append(max(faces, key=lambda f: f.area).embedding)
        if embs:
            out[person.name] = embs
        done += len(embs)
        if n % 100 == 0 or n == len(people):
            print(f"  ... {n}/{len(people)} shaxs, {done} ta surat", flush=True)
    return out


def _load_or_embed(a) -> dict[str, list[np.ndarray]]:
    if a.cache and a.cache.exists():
        z = np.load(a.cache, allow_pickle=True)
        print(f"Kesh: {a.cache}")
        return {k: list(v) for k, v in z.items()}
    data = embed_dataset(a.dataset, a.max_per_person)
    if a.cache:
        np.savez_compressed(a.cache, **{k: np.stack(v) for k, v in data.items()})
        print(f"Kesh yozildi: {a.cache}")
    return data


def identification(data: dict[str, list[np.ndarray]], threshold: float, margin: float,
                   templates_per_user: int = 1) -> tuple[float, float, float]:
    """1:N qidiruv: to'g'ri, XATO shaxs, rad etilgan ulushlari.

    templates_per_user — galereyada har bir shaxsdan nechta surat (tizimimizda 1 dan ko'p)."""
    people = [p for p in data if len(data[p]) > templates_per_user]
    gallery, owners = [], []
    for p in people:
        for v in data[p][:templates_per_user]:
            gallery.append(v)
            owners.append(p)
    G = np.stack(gallery)
    owners = np.array(owners)
    correct = wrong = rejected = 0
    for p in people:
        for probe in data[p][templates_per_user:]:
            sims = G @ probe
            best = {}
            for owner, sim in zip(owners, sims):  # har bir shaxs bo'yicha eng yaxshi ball
                if owner not in best or sim > best[owner]:
                    best[owner] = float(sim)
            ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
            top, second = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
            if top[1] < threshold or top[1] - second < margin:
                rejected += 1
            elif top[0] == p:
                correct += 1
            else:
                wrong += 1
    total = max(correct + wrong + rejected, 1)
    return correct / total, wrong / total, rejected / total


def sweep(data: dict[str, list[np.ndarray]]) -> None:
    print(f"\n{'chegara':>8} {'margin':>7} {'shablon':>8} {'to.gri':>8} {'XATO':>8} {'rad':>8}")
    for templates in (1, 3):
        for threshold in (0.40, 0.45, 0.50, 0.55, 0.60):
            for margin in (0.05, 0.08, 0.12, 0.16):
                c, w, r = identification(data, threshold, margin, templates)
                print(f"{threshold:>8.2f} {margin:>7.2f} {templates:>8} {c:>7.1%} {w:>7.2%} {r:>7.1%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--max-per-person", type=int, default=10)
    ap.add_argument("--impostor-pairs", type=int, default=20000)
    ap.add_argument("--margin", type=float, default=0.08)
    ap.add_argument("--out", type=Path, default=Path("natijalar.csv"))
    ap.add_argument("--cache", type=Path, default=None,
                    help="embeddinglarni .npz ga saqlab qo'yadi; keyingi ishga tushirishda qayta hisoblanmaydi")
    ap.add_argument("--sweep", action="store_true", help="chegara va margin kombinatsiyalarini sinaydi")
    a = ap.parse_args()

    data = _load_or_embed(a)
    people = list(data)
    print(f"Shaxslar: {len(people)}, suratlar: {sum(len(v) for v in data.values())}")

    genuine = np.array([float(x @ y) for embs in data.values() for x, y in itertools.combinations(embs, 2)])
    rng = random.Random(0)
    impostor = []
    for _ in range(a.impostor_pairs):
        p, q = rng.sample(people, 2)
        impostor.append(float(rng.choice(data[p]) @ rng.choice(data[q])))
    impostor = np.array(impostor)
    print(f"Haqiqiy juftlar: {len(genuine)}, soxta juftlar: {len(impostor)}")

    rows = []
    for t in np.arange(0.20, 0.80, 0.025):
        far = float((impostor >= t).mean())
        frr = float((genuine < t).mean())
        rows.append((round(float(t), 3), far, frr))
    eer_row = min(rows, key=lambda r: abs(r[1] - r[2]))

    corr, wrong, rej = identification(data, 0.45, a.margin)

    print(f"\n{'chegara':>8} {'FAR':>10} {'FRR':>10}")
    for t, far, frr in rows:
        mark = "  <- EER" if (t, far, frr) == eer_row else ("  <- joriy sozlama" if abs(t - 0.45) < 1e-6 else "")
        print(f"{t:>8.3f} {far:>10.5f} {frr:>10.5f}{mark}")
    print(f"\nEER ~ {(eer_row[1] + eer_row[2]) / 2:.4f} (chegara {eer_row[0]})")
    print(f"1:N (chegara 0.45, margin {a.margin}): to'g'ri {corr:.3%}, "
          f"XATO shaxs {wrong:.3%}, rad etildi {rej:.3%}")
    if a.sweep:
        sweep(data)

    with a.out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["threshold", "FAR", "FRR"])
        w.writerows(rows)
    print(f"CSV: {a.out}")
    _plot(rows, genuine, impostor, a.out.with_suffix(".png"))


def _plot(rows, genuine, impostor, path: Path) -> None:
    """Diplomning 'Tajriba natijalari' bo'limi uchun ikkita grafik."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    t = [r[0] for r in rows]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2))
    ax1.plot(t, [r[1] for r in rows], label="FAR (begonani qabul qilish)")
    ax1.plot(t, [r[2] for r in rows], label="FRR (o'zini rad etish)")
    ax1.axvline(0.45, ls="--", lw=1, color="gray")
    ax1.annotate("joriy chegara 0.45", (0.45, 0.5), rotation=90, va="center", fontsize=8, color="gray")
    ax1.set_xlabel("Chegara (cosine)")
    ax1.set_ylabel("Xato ulushi")
    ax1.set_yscale("log")
    ax1.legend(fontsize=9)
    ax1.set_title("FAR va FRR")
    ax2.hist(impostor, bins=60, alpha=0.65, label="turli odamlar", density=True)
    ax2.hist(genuine, bins=60, alpha=0.65, label="bir odam", density=True)
    ax2.axvline(0.45, ls="--", lw=1, color="gray")
    ax2.set_xlabel("Cosine o'xshashlik")
    ax2.set_title("O'xshashliklar taqsimoti")
    ax2.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print(f"Grafik: {path}")


if __name__ == "__main__":
    main()
