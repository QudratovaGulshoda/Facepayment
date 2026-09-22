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
    analyzer = InsightFaceAnalyzer(use_mediapipe=False)
    out: dict[str, list[np.ndarray]] = {}
    for person in sorted(p for p in root.iterdir() if p.is_dir()):
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


if __name__ == "__main__":
    main()
