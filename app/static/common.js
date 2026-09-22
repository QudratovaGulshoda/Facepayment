// FacePay: mijoz sayti va kassa uchun umumiy kod (kamera, liveness kadrlari, xabarlar)
"use strict";

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const b64 = (buf) => { let s = ""; for (const b of new Uint8Array(buf)) s += String.fromCharCode(b); return btoa(s); };
const fmt = (n) => Number(n).toLocaleString("ru-RU").replace(/,/g, " ").replace(/ /g, " ");
const phoneVal = (sel) => $(sel).value.replace(/[\s()-]/g, "");

const MESSAGES = {
  yuz_tanilmadi: "Yuz tanilmadi. Ro'yxatdan o'tganmisiz?",
  noaniq_moslik: "Kim to'layotgani aniq emas (o'xshash odam bor). Boshqa usul bilan to'lang.",
  liveness_faol: "Harakatlar to'g'ri bajarilmadi. Ko'rsatmalarga amal qilib, qayta urining.",
  "liveness_faol:bajarilmadi:turn_left": "Boshingizni chapga burganingiz aniqlanmadi. Aniqroq (yelkangiz tomon) buring.",
  "liveness_faol:bajarilmadi:turn_right": "Boshingizni o'ngga burganingiz aniqlanmadi. Aniqroq (yelkangiz tomon) buring.",
  "liveness_faol:bajarilmadi:blink": "Ko'z qisish aniqlanmadi. Ko'zingizni to'liq yumib, keyin oching.",
  "liveness_faol:bajarilmadi:open_mouth": "Og'iz ochish aniqlanmadi. Og'zingizni kengroq oching.",
  "liveness_faol:boshida_togri_qaramadi": "Boshida kameraga to'g'ri qarang.",
  liveness_passiv: "Tirik odam ekanligi tasdiqlanmadi (rasm yoki ekran bo'lishi mumkin).",
  kadrlarda_turli_shaxs: "Kadrlarda turli odamlar aniqlandi.",
  bir_nechta_yuz: "Kadrda bir nechta yuz bor. Yolg'iz turing.",
  yuz_topilmadi: "Yuz topilmadi. Kameraga yaqinroq keling.",
  yuz_markazda_emas: "Yuzingizni kadr markaziga keltiring.",
  sifat_past: "Kadr sifati past (yorug'lik yoki xiralik). Yorug' joyda qayta urining.",
  old_kadrlar_kam: "Boshida va oxirida kameraga to'g'ri qarang.",
  kadrlar_soni_notogri: "Kadrlar yetarli emas. Qayta urining.",
  mablag_yetarli_emas: "Mablag' yetarli emas. Kartangizni ulang.",
  kartada_mablag_yetarli_emas: "Kartada mablag' yetarli emas.",
  kunlik_limit: "Kunlik limitdan oshdi.",
  pin_notogri: "PIN noto'g'ri.",
  pin_urinishlar_tugadi: "PIN urinishlari tugadi, to'lov bekor qilindi.",
  pin_vaqti_tugadi: "PIN kiritish vaqti tugadi.",
  vaqtincha_bloklangan: "Hisob vaqtincha bloklangan (15 daqiqa). PIN'ni yuz orqali tiklashingiz mumkin.",
  sinov_yaroqsiz: "Sinov muddati tugadi. Qayta urining.",
  telefon_royxatda: "Bu telefon allaqachon ro'yxatdan o'tgan.",
  yuz_allaqachon_royxatda: "Bu yuz allaqachon boshqa hisobga bog'langan.",
  rozilik_berilmagan: "Rozilik belgisini qo'ying.",
  yuz_mos_emas: "Yuz hisob egasiga mos kelmadi.",
  boshqa_shaxsga_oxshash: "Bu ko'rinish boshqa foydalanuvchiga juda o'xshash.",
  topilmadi: "Bu telefon bilan ro'yxatdan o'tilmagan.",
  karta_topilmadi: "Karta topilmadi.",
  sms_kod_notogri: "SMS kod noto'g'ri.",
  sms_urinishlar_tugadi: "SMS urinishlari tugadi. Kartani qaytadan ulang.",
  karta_raqami_notogri: "Karta raqami noto'g'ri.",
  faqat_uzcard_humo: "Faqat Uzcard (8600, 5614) va Humo (9860) kartalari.",
  muddat_notogri: "Amal qilish muddati noto'g'ri (OOYY).",
  kartalar_soni_limit: "Ko'pi bilan 3 ta karta ulash mumkin.",
  terminal_nomalum: "Kassa ro'yxatdan o'tmagan. \"Sozlash\" bo'limini oching.",
  terminal_sozlanmagan: "Kassa ro'yxatdan o'tmagan. \"Sozlash\" bo'limini oching.",
  imzo_xatosi: "So'rov imzosi xato. Qurilma soatini tekshiring.",
  juda_kop_sorov: "Juda ko'p so'rov. Bir daqiqa kuting.",
  admin_emas: "Admin kaliti noto'g'ri.",
  kamera: "Kameraga ruxsat berilmadi yoki kamera topilmadi.",
  ed25519: "Brauzeringiz eskirgan (Ed25519 yo'q). Chrome, Safari yoki Firefox'ning yangi versiyasini oching.",
  tarmoq: "Server bilan aloqa yo'q. Internetni tekshiring va qayta urining.",
};

const FIELD_ERRORS = {
  pin: "PIN 4–6 ta raqam bo'lishi kerak (1234, 1111 kabi oddiy PIN qabul qilinmaydi).",
  new_pin: "PIN 4–6 ta raqam bo'lishi kerak (1234, 1111 kabi oddiy PIN qabul qilinmaydi).",
  number: "Karta raqamini to'liq kiriting (16 ta raqam).",
  expire: "Amal qilish muddatini OOYY ko'rinishida kiriting (masalan 0829).",
  phone: "Telefonni +998901234567 ko'rinishida kiriting.",
  full_name: "Ism familiyani kiriting.",
  code: "SMS kodni kiriting.",
  amount: "Summani kiriting.",
  label: "Nomini kiriting.",
  consent: "Rozilik belgisini qo'ying.",
  name: "Nomini kiriting.",
};

function errText(code) {
  if (!code) return "Noma'lum xato";
  const base = String(code).split(":")[0];
  return MESSAGES[code] || MESSAGES[base] || code;
}

function codeOf(res) {
  const d = res.data || {};
  if (d.error) return d.error;
  if (typeof d.detail === "string") return d.detail;
  if (Array.isArray(d.detail)) {
    return [...new Set(d.detail.map((e) => e.loc[e.loc.length - 1]))].map((f) => FIELD_ERRORS[f] || f).join(" ");
  }
  return "HTTP " + res.status;
}

function show(sel, kind, text) {
  const el = $(sel);
  el.hidden = false;
  el.className = "result " + kind;
  el.textContent = text;
}

async function postJSON(path, payload, headers = {}) {
  try {
    const r = await fetch(path, { method: "POST", body: JSON.stringify(payload),
      headers: { "Content-Type": "application/json", ...headers } });
    return { status: r.status, data: await r.json().catch(() => ({})) };
  } catch {
    return { status: 0, data: { error: "tarmoq" } };
  }
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { console.error(e); alert(String(e)); } finally { btn.disabled = false; }
}

function remember(key, value) { try { localStorage.setItem(key, value); } catch {} }
function recall(key) { try { return localStorage.getItem(key) || ""; } catch { return ""; } }

// ---------- Kamera va liveness kadrlari ----------
const Camera = {
  stream: null,
  video: null,
  prompt: null,

  attach(videoEl, promptEl) { this.video = videoEl; this.prompt = promptEl; },

  async start() {
    if (this.stream) return;
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: false,
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } } });
    this.video.srcObject = this.stream;
    await this.video.play();
    this.prompt.textContent = "Tayyor";
  },

  stop() {
    if (!this.stream) return;
    this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    this.video.srcObject = null;
  },

  // Kadrlar xom (ko'zgu emas) holda yuboriladi — server shunga kalibrlangan.
  // Harakatlar uchun 2.5 s, boshida/oxirida 1.2 s; ~7-8 kadr/s — ko'z qisish ikki kadr orasida qolmasin.
  async capture(instructions) {
    const plan = [["Kameraga to'g'ri qarang", 1200], ...instructions.map((t) => [t, 2500]), ["Kameraga to'g'ri qarang", 1200]];
    const total = plan.reduce((a, [, ms]) => a + ms, 0), maxFrames = 56;
    const interval = Math.max(110, total / maxFrames);
    const v = this.video, scale = Math.min(1, 640 / v.videoWidth);
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(v.videoWidth * scale);
    canvas.height = Math.round(v.videoHeight * scale);
    const ctx = canvas.getContext("2d");
    const frames = [], timestamps_ms = [];
    for (const [text, ms] of plan) {
      this.prompt.textContent = text;
      const end = performance.now() + ms;
      while (performance.now() < end) {
        if (frames.length < maxFrames) {
          ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
          frames.push(canvas.toDataURL("image/jpeg", 0.8).split(",")[1]);
          timestamps_ms.push(Date.now());
        }
        await sleep(interval);
      }
    }
    this.prompt.textContent = "Tekshirilmoqda…";
    return { frames, timestamps_ms };
  },

  // getChallenge() -> {status, data}; submit(capture) -> {status, data}
  async run(getChallenge, submit) {
    try { await this.start(); } catch { return { status: 0, data: { error: "kamera" } }; }
    const ch = await getChallenge();
    if (ch.status !== 200) return ch;
    const cap = await this.capture(ch.data.instructions);
    const res = await submit({ challenge_id: ch.data.challenge_id, ...cap });
    this.prompt.textContent = "Tayyor";
    return res;
  },
};
