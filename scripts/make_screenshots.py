"""Hujjat uchun skrinshotlar tayyorlaydi (docs/screens/*.png).

Har bir skrinshot — loyihaning HAQIQIY sahifasi, namunaviy ma'lumotlar bilan to'ldirilgan holda.
Kamera oynasi o'rniga chizma qo'yiladi (brauzer serverda ishlagani uchun kamera yo'q).

    .venv/bin/python scripts/make_screenshots.py
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
OUT = ROOT / "docs" / "screens"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CAMERA_PLACEHOLDER = """
<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
            background:linear-gradient(160deg,#2b3a44,#16202a)">
  <svg width="120" height="120" viewBox="0 0 24 24" fill="none" stroke="#7f98a8" stroke-width="1.2">
    <circle cx="12" cy="8.5" r="4"/><path d="M4 21c0-4.2 3.6-6.5 8-6.5s8 2.3 8 6.5"/>
  </svg>
</div>
"""


def build_page(page: str, state_js: str, camera_prompt: str | None = None) -> str:
    """Sahifani bitta faylga yig'adi (CSS va JS ichkariga), keyin holatni o'rnatadi."""
    html = (STATIC / page).read_text()
    css = (STATIC / "style.css").read_text()
    common = (STATIC / "common.js").read_text()
    html = html.replace('<link rel="stylesheet" href="/static/style.css">', f"<style>{css}</style>")
    html = html.replace('<script src="/static/common.js"></script>', f"<script>{common}</script>")
    cam = CAMERA_PLACEHOLDER if camera_prompt is not None else ""
    prompt = camera_prompt or ""
    state = f"""
<script>
window.addEventListener("load", () => {{
  document.documentElement.dataset.theme = "light";   // hujjat uchun yorug' mavzu
  // skrinshot uchun holatni o'rnatamiz (sahifaning o'z mantig'iga tegilmaydi)
  const cam = document.querySelector(".cam");
  if (cam) {{
    if ({json.dumps(camera_prompt is not None)}) {{
      cam.hidden = false;
      cam.insertAdjacentHTML("afterbegin", {json.dumps(cam)});
      cam.querySelector(".prompt").textContent = {json.dumps(prompt)};
    }} else {{
      cam.hidden = true;
    }}
  }}
  try {{ {state_js} }} catch (e) {{ document.title = "XATO: " + e.message; }}
}});
</script>
"""
    return html.replace("</body>", state + "</body>")


def shoot(name: str, page: str, state_js: str, camera_prompt: str | None = None,
          width: int = 760, height: int = 1100) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    # Chrome macOS ning /var/folders ichidagi vaqtinchalik papkasini o'qiy olmaydi —
    # shuning uchun sahifani loyiha ichida yaratamiz
    tmp = ROOT / ".tmp_screens"
    tmp.mkdir(exist_ok=True)
    f = tmp / f"{name}.html"
    f.write_text(build_page(page, state_js, camera_prompt))
    target = OUT / f"{name}.png"
    # Alohida profil: foydalanuvchining ochiq Chrome oynasi bilan to'qnashmasligi uchun.
    # Chrome rasmni yozgandan keyin ba'zan o'zi yopilmaydi — shuning uchun kutamiz va to'xtatamiz.
    profile = ROOT / ".tmp_screens" / f"profile-{name}"
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                             "--virtual-time-budget=3000", f"--screenshot={target}",
                             f"--window-size={width},{height}", f"file://{f}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if target.exists() and proc.poll() is None:
            time.sleep(0.4)      # yozib tugatishiga ulgursin
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    f.unlink()
    if not target.exists():
        raise RuntimeError(f"{name}: skrinshot yaratilmadi")
    _trim(target)
    return target


def _trim(path: Path, pad: int = 24) -> None:
    """Pastdagi bo'sh joyni kesib tashlaydi (fon rangi bilan bir xil qatorlar)."""
    import cv2
    import numpy as np

    img = cv2.imread(str(path))
    if img is None:
        return
    bg = img[-1, -1]
    rows = np.where(np.abs(img.astype(int) - bg.astype(int)).sum(axis=(1, 2)) > 2000)[0]
    if rows.size:
        cv2.imwrite(str(path), img[: min(img.shape[0], rows[-1] + pad)])


# Umumiy yordamchi JS parchalari
def tab(name: str) -> str:
    return f'selectTab("{name}");'


def fill(values: dict[str, str]) -> str:
    return "".join(f'document.querySelector("{k}").value = {json.dumps(v)};' for k, v in values.items())


def result(sel: str, kind: str, text: str) -> str:
    return f'show("{sel}", "{kind}", {json.dumps(text)});'


SHOTS: list[tuple[str, str, str, str | None]] = [
    # --- Mijoz kabineti ---
    ("01-kabinet-royxat", "index.html", tab("enroll") + fill({
        "#en-phone": "+998901234567", "#en-name": "Gulshoda Qudratova", "#en-pin": "4821"}) +
     'document.querySelector("#en-consent").checked = true;', None),
    ("02-kabinet-kamera", "index.html", tab("enroll") + fill({
        "#en-phone": "+998901234567", "#en-name": "Gulshoda Qudratova"}), "Ko'zingizni qising"),
    ("03-kabinet-royxat-natija", "index.html", tab("enroll") + fill({
        "#en-phone": "+998901234567", "#en-name": "Gulshoda Qudratova"}) +
     result("#en-result", "ok", "✓ Ro'yxatdan o'tdingiz. Endi \"Kartalarim\" bo'limida kartangizni ulang."), None),
    ("04-kabinet-karta", "index.html", tab("cards") + fill({
        "#cd-phone": "+998901234567", "#cd-pin": "4821", "#cd-number": "8600123456789012", "#cd-exp": "0829"}), None),
    ("05-kabinet-karta-sms", "index.html", tab("cards") + fill({"#cd-phone": "+998901234567"}) +
     'document.querySelector("#cd-verify").hidden = false;' + fill({"#cd-code": "666666"}) +
     result("#cd-result", "warn", "8600 **** **** 9012: demo rejim — haqiqiy SMS yuborilmaydi. Kodni kiriting: 666666"), None),
    ("06-kabinet-kartalar", "index.html", tab("cards") + fill({"#cd-phone": "+998901234567"}) + '''
     const ul = document.querySelector("#cd-list");
     ul.innerHTML = `<li><span><span class="mono">8600 **** **** 9012</span> <span class="tag ok">asosiy</span></span>
       <span><button class="link">Asosiy qilish</button><button class="link" style="color:var(--err);margin-left:12px">O'chirish</button></span></li>
       <li><span><span class="mono">9860 **** **** 9015</span></span>
       <span><button class="link">Asosiy qilish</button><button class="link" style="color:var(--err);margin-left:12px">O'chirish</button></span></li>`;''', None),
    ("07-kabinet-tarix", "index.html", tab("history") + fill({"#hs-phone": "+998901234567"}) + '''
     document.querySelector("#hs-list").innerHTML = `
      <li><span><div>Do'kon "Mehr"</div><div class="meta">08.10 14:32 · 8600 **** **** 9012</div></span>
          <span><span class="amount">67 000</span> <span class="tag ok">to'landi</span></span></li>
      <li><span><div>Kafe "Bahor"</div><div class="meta">08.10 12:05 · 8600 **** **** 9012</div></span>
          <span><span class="amount">25 000</span> <span class="tag ok">to'landi</span></span></li>
      <li><span><div>Do'kon "Mehr"</div><div class="meta">07.10 19:41 · 8600 **** **** 9012</div></span>
          <span><span class="amount">350 000</span> <span class="tag ok">to'landi</span></span></li>
      <li><span><div>Kafe "Bahor"</div><div class="meta">07.10 10:14 · Yuz tanilmadi</div></span>
          <span><span class="amount">15 000</span> <span class="tag err">rad etildi</span></span></li>`;
     ul.querySelectorAll("li").forEach((li, i) => {
       if (i > 2) return;
       const b = document.createElement("button");
       b.className = "link"; b.style.color = "var(--err)"; b.textContent = "Bu men emasman";
       li.lastElementChild.appendChild(b);
     });''', None),
    ("08-kabinet-sozlamalar", "index.html", tab("settings") + fill({
        "#st-phone": "+998901234567", "#rp-pin": "7392", "#va-pin": "4821"}), None),

    ("08b-kabinet-pauza", "index.html", tab("settings")
     + fill({"#fz-pin": "1234"})
     + result("#fz-result", "warn",
              "Yuz orqali to'lov to'xtatildi. Endi hech kim sizning yuzingiz bilan to'lay olmaydi."), None),

    # --- Mijoz ekrani (do'kondagi kamerali qurilma) ---
    ("09-ekran-sozlash", "ekran.html", 'screen("setup");', None),
    ("10-ekran-ulash-kodi", "ekran.html", '''screen("idle");
     document.querySelector("#pair-box").hidden = false;
     document.querySelector("#pair-code").textContent = "482915";''', None),
    ("11-ekran-kutish", "ekran.html", 'screen("idle");', None),
    ("12-ekran-tolov", "ekran.html", '''screen("request");
     document.querySelector("#rq-merchant").textContent = "Do'kon \\"Mehr\\"";
     document.querySelector("#rq-amount").textContent = "67 000 so'm";''', "Boshingizni chapga buring"),
    ("13-ekran-pin", "ekran.html", '''screen("pin-screen");
     document.querySelector("#pin-title").textContent = "G****** Q., PIN kiriting";
     document.querySelector("#pin").value = "••••";''', None),
    ("14-ekran-tolandi", "ekran.html", '''screen("done");
     const t = document.querySelector("#done-title"); t.className = "outcome ok"; t.textContent = "✓ To'landi";
     document.querySelector("#done-detail").textContent = "67 000 so'm · 8600 **** **** 9012. Rahmat!";''', None),
    ("15-ekran-rad", "ekran.html", '''screen("done");
     const t = document.querySelector("#done-title"); t.className = "outcome err"; t.textContent = "✗ Rad etildi";
     document.querySelector("#done-detail").textContent = "Tirik odam ekanligi tasdiqlanmadi (rasm yoki ekran bo'lishi mumkin).";''', None),

    # --- Kassa ---
    ("16-kassa-ulash", "kassa.html", '''document.querySelector("#setup").hidden = false;
     document.querySelector("#kassa").hidden = true;
     document.querySelector("#pair-code").value = "482915";''', None),
    ("17-kassa-kutilmoqda", "kassa.html", '''document.querySelector("#setup").hidden = true;
     document.querySelector("#kassa").hidden = false;
     document.querySelector("#merchant-name").textContent = "Do'kon \\"Mehr\\"";
     document.querySelector("#amount").value = 67000;
     status("", "Mijoz ekranida kutilmoqda…", "67 000 so'm");
     document.querySelector("#cancel-btn").hidden = false;
     document.querySelector("#today").textContent = "442 000 so'm";''', None),
    ("18-kassa-tolandi", "kassa.html", '''document.querySelector("#setup").hidden = true;
     document.querySelector("#kassa").hidden = false;
     document.querySelector("#merchant-name").textContent = "Do'kon \\"Mehr\\"";
     document.querySelector("#amount").value = 67000;
     status("ok", "✓ To'landi: 67 000 so'm", "G****** Q. · yuz");
     document.querySelector("#today").textContent = "509 000 so'm";
     document.querySelector("#tx-list").innerHTML = `
      <li><span><div>14:32</div><div class="meta">yuz</div></span><span><span class="amount">67 000</span> <span class="tag ok">✓</span></span></li>
      <li><span><div>13:58</div><div class="meta">yuz + PIN</div></span><span><span class="amount">350 000</span> <span class="tag ok">✓</span></span></li>
      <li><span><div>12:05</div><div class="meta">yuz</div></span><span><span class="amount">25 000</span> <span class="tag ok">✓</span></span></li>
      <li><span><div>10:14</div><div class="meta">Yuz tanilmadi</div></span><span><span class="amount">15 000</span> <span class="tag err">✗</span></span></li>`;
     document.querySelectorAll("#tx-list li").forEach((li, i) => {
       if (i > 2) return;
       const b = document.createElement("button");
       b.className = "link"; b.style.marginLeft = "10px"; b.textContent = "Qaytarish";
       li.lastElementChild.appendChild(b);
     });''', None),

    # --- Kassirsiz tezkor terminal ---
    ("19-terminal-sozlash", "turniket.html", '''document.querySelector("#setup").hidden = false;
     document.querySelector("#gate-ui").hidden = true;''', None),
    ("20-terminal-tolandi", "turniket.html", '''document.querySelector("#setup").hidden = true;
     document.querySelector("#gate-ui").hidden = false;
     document.querySelector("#station").textContent = "Tezkor terminal";
     document.querySelector("#fare").textContent = "10 000 so'm";
     gate("ok", "✓", "To'landi, rahmat", "G****** Q. · 10 000 so'm");
     document.querySelector("#count").textContent = "37";
     document.querySelector("#total").textContent = "370 000";''', "Kameraga to'g'ri qarang"),
]


def admin_shot(stats: dict) -> None:
    js = f'''
     document.querySelector("#login").hidden = true;
     document.querySelector("#panel").hidden = false;
     const d = {json.dumps(stats)};
     document.querySelector("#gateway").textContent = "demo rejim (pul yechilmaydi)";
     document.querySelector("#today-ok").textContent = d.today.approved;
     document.querySelector("#today-no").textContent = d.today.declined;
     document.querySelector("#today-sum").textContent = fmt(d.today.amount);
     document.querySelector("#users").textContent = d.users;
     document.querySelector("#templates").textContent = d.templates_in_memory;
     document.querySelector("#cards").textContent = d.cards;
     document.querySelector("#total").textContent = d.total_transactions;
     rows("#terminals", Object.entries(d.terminals).map(([r, n]) => [ROLE_UZ[r] || r, n]).concat([["Sotuvchilar", d.merchants]]));
     rows("#reasons", d.decline_reasons.map((x) => [errText(x.reason), x.count]));
     document.querySelector("#audit-n").textContent = d.audit.entries;
     const a = document.querySelector("#audit-ok"); a.textContent = "✓ buzilmagan"; a.className = "v ok";'''
    shoot("21-admin-panel", "admin.html", js, None, 760, 1500)


if __name__ == "__main__":
    if not Path(CHROME).exists():
        raise SystemExit("Google Chrome topilmadi")
    if OUT.exists():
        shutil.rmtree(OUT)
    for name, page, js, cam in SHOTS:
        shoot(name, page, js, cam)
        print("✓", name)
    stats = json.loads((ROOT / "docs" / "admin_stats.json").read_text()) if (ROOT / "docs" / "admin_stats.json").exists() else {
        "users": 2, "templates_in_memory": 4, "cards": 2, "merchants": 3,
        "terminals": {"payment": 2, "cashier": 1, "enroll": 1, "portal": 1},
        "today": {"approved": 12, "declined": 2, "amount": 509000}, "total_transactions": 58,
        "decline_reasons": [{"reason": "yuz_tanilmadi", "count": 4}, {"reason": "liveness_passiv", "count": 2},
                            {"reason": "kartada_mablag_yetarli_emas", "count": 1}],
        "audit": {"intact": True, "first_broken_id": None, "entries": 142}}
    admin_shot(stats)
    print("✓ 21-admin-panel")
    shutil.rmtree(ROOT / ".tmp_screens", ignore_errors=True)
    print(f"Skrinshotlar: {OUT}")
