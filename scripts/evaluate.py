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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--max-per-person", type=int, default=10)
    ap.add_argument("--impostor-pairs", type=int, default=20000)
    ap.add_argument("--margin", type=float, default=0.08)
    ap.add_argument("--out", type=Path, default=Path("natijalar.csv"))
    a = ap.parse_args()

    data = embed_dataset(a.dataset, a.max_per_person)
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

    # 1:N: har bir shaxsning birinchi surati galereyada, qolganlari so'rov
    gallery = np.stack([data[p][0] for p in people])
    correct = wrong = rejected = 0
    for k, p in enumerate(people):
        for probe in data[p][1:]:
            sims = gallery @ probe
            order = np.argsort(-sims)
            top, second = sims[order[0]], (sims[order[1]] if len(order) > 1 else 0)
            if top < 0.45 or top - second < a.margin:
                rejected += 1
            elif order[0] == k:
                correct += 1
            else:
                wrong += 1
    total = max(correct + wrong + rejected, 1)

    print(f"\n{'chegara':>8} {'FAR':>10} {'FRR':>10}")
    for t, far, frr in rows:
        mark = "  <- EER" if (t, far, frr) == eer_row else ("  <- joriy sozlama" if abs(t - 0.45) < 1e-6 else "")
        print(f"{t:>8.3f} {far:>10.5f} {frr:>10.5f}{mark}")
    print(f"\nEER ~ {(eer_row[1] + eer_row[2]) / 2:.4f} (chegara {eer_row[0]})")
    print(f"1:N (chegara 0.45, margin {a.margin}): to'g'ri {correct / total:.3%}, "
          f"XATO shaxs {wrong / total:.3%}, rad etildi {rejected / total:.3%}")

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
