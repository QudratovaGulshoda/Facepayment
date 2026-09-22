# FacePay — yuz orqali to'lov tizimi

Karta yoki telefonsiz, faqat yuz orqali to'lov (metro turniketi, do'kon kassasi, avtobus).
Bitiruv malakaviy ishi uchun loyiha.

## Asosiy imkoniyatlar

| Talab | Qanday amalga oshirilgan |
|---|---|
| **Yuzni tanish** | InsightFace: SCRFD (yuz topish) + ArcFace buffalo_l (512 o'lchamli vektor) |
| **Tirik odamni tekshirish** | Faol sinov (server tasodifiy tanlagan harakatlar: ko'z qisish, bosh burish, og'iz ochish) + passiv MiniFASNetV2 modeli |
| **Yosh o'zgarishi** | Bir odamda bir nechta shablon, ishonchli to'lovlardan keyin shablon asta-sekin yangilanadi (EMA), ball pasaysa qayta ro'yxatdan o'tish taklif qilinadi |
| **Makiyaj, ko'zoynak, soqol** | Qo'shimcha "variant" shablon (PIN bilan tasdiqlanadi) + yangi ko'rinish avtomatik alohida shablon sifatida qo'shiladi |
| **Shaxsiy ma'lumot himoyasi** | Rasm saqlanmaydi. Shablon maxfiy ortogonal matritsa bilan o'zgartiriladi va AES-256-GCM bilan shifrlanadi. Ism va telefon shifrlangan |
| **Terminal xavfsizligi** | Har bir terminal Ed25519 kaliti bilan so'rovni imzolaydi, vaqt tamg'asi va nonce orqali takroriy so'rov (replay) bloklanadi |
| **Xato to'lovning oldini olish** | 1:N qidiruvda 1-o'rin va 2-o'rin orasida farq (margin) talab qilinadi, katta summa yoki chegaradagi ballda PIN so'raladi |

## Arxitektura

```
 ┌──────────────────────┐   HTTPS + Ed25519 imzo    ┌───────────────────────────────────────┐
 │ Terminal (turniket)  │ ────────────────────────▶ │ FastAPI server                         │
 │  kamera, ekran       │  1. sinov so'raydi         │  ├─ terminal_auth  (imzo, nonce, limit)│
 │  yopiq kalit (.pem)  │  2. kadrlarni yuboradi     │  ├─ pipeline                           │
 └──────────────────────┘  3. natija / PIN           │  │   sifat → liveness → shaxs          │
                                                     │  │   izchilligi → embedding            │
                                                     │  ├─ matcher  (1:N, margin, moslashish) │
                                                     │  ├─ payments (limitlar, PIN, balans)   │
                                                     │  └─ audit    (xesh-zanjirli jurnal)    │
                                                     └──────────────┬────────────────────────┘
                                                                    │ faqat shifrlangan ma'lumot
                                                              ┌─────▼─────┐
                                                              │ Ma'lumotlar│
                                                              │ bazasi     │
                                                              └───────────┘
```

### To'lov jarayoni

1. Terminal `POST /v1/liveness/challenge` so'rovini yuboradi. Server tasodifiy harakatlarni qaytaradi, masalan `["blink", "turn_right"]`. Sinov 30 soniya amal qiladi va faqat bir marta ishlatiladi.
2. Ekranda "Ko'zingizni qising → Boshingizni o'ngga buring" chiqadi. Kamera taxminan 5 soniyada 10–38 ta kadr oladi.
3. `POST /v1/payments` so'rovi kadrlarni, summani va idempotentlik kalitini olib boradi. Server quyidagilarni bajaradi:
   - har bir kadrda **bitta asosiy yuz** borligini tekshiradi (orqada turgan boshqa yo'lovchi to'lovchi deb olinmaydi);
   - harakatlar **to'g'ri tartibda** bajarilganini tekshiradi;
   - **har bir juft kadr** bir odamga tegishli ekanini tekshiradi (hujumchi kadrlar orasida yuzni almashtira olmaydi);
   - kadr sifatini (xiralik, yorug'lik, burilish) va passiv liveness'ni baholaydi;
   - 1:N qidiruv o'tkazib, chegara va margin qoidasini qo'llaydi.
4. Natija quyidagicha:
   - summa ≤ 200 000 so'm va moslik ishonchli bo'lsa: **faqat yuz bilan tasdiqlanadi**;
   - aks holda `pending_pin` qaytadi va 60 soniya ichida PIN kiritiladi (`POST /v1/payments/{id}/pin`).

## Xavfsizlik va maxfiylik (batafsil)

### Biometrik shablonni himoyalash: 3 qatlam
1. **Rasm saqlanmaydi.** Kadrlar faqat xotirada qayta ishlanadi, diskka ham, jurnalga ham yozilmaydi.
2. **Bekor qilinadigan biometriya (cancelable biometrics).** ArcFace vektori maxfiy ortogonal matritsaga ko'paytiriladi. Cosine o'xshashlik o'zgarmaydi, shuning uchun aniqlik yo'qolmaydi. Saqlangan vektor esa asl vektorga o'xshamaydi, boshqa tizimlarda ishlatib bo'lmaydi. Ma'lumot sizib chiqsa, urug' (seed) almashtiriladi va barcha eski shablonlar yaroqsiz bo'ladi.
3. **AES-256-GCM.** AAD sifatida `foydalanuvchi_id + shablon_id` ishlatiladi. Hujumchi bazada A foydalanuvchining shablonini B ga ko'chirsa, deshifrlash muvaffaqiyatsiz bo'ladi.

### Shaxsiy ma'lumotlar
- Ism va telefon AES-GCM bilan shifrlangan. Telefon bo'yicha qidirish HMAC-SHA256 orqali ishlaydi, ochiq qiymat bazada yo'q.
- PIN scrypt bilan xeshlanadi (N=2¹⁴, har safar yangi tuz). 5 marta noto'g'ri kiritilsa, hisob 15 daqiqaga bloklanadi. `1111`, `1234` kabi oddiy PIN'lar qabul qilinmaydi.
- Terminal ekranida ism niqoblanib ko'rsatiladi: `G****** K.`
- Audit jurnalida faqat ID va hodisa kodlari saqlanadi. Har bir yozuv oldingisining SHA-256 xeshini o'z ichiga oladi, shuning uchun jurnalni o'zgartirish sezilib qoladi (`GET /v1/admin/audit/verify`).
- **Unutilish huquqi:** `POST /v1/users/delete` so'rovi (PIN + yuz bilan tasdiqlanadi) shablonlar va shaxsiy ma'lumotlarni butunlay o'chiradi.
- **Rozilik:** ro'yxatdan o'tishda aniq rozilik (`consent: true`) talab qilinadi.

### Qonunchilik (O'zbekiston)
- "Shaxsga doir ma'lumotlar to'g'risida"gi Qonun (O'RQ-547, 2019): rozilik talab qilinadi, ma'lumotni o'chirish huquqi bor, himoya choralari ko'rilishi shart.
- O'zbekiston fuqarolarining shaxsiy ma'lumotlari O'zbekiston hududidagi serverlarda saqlanishi shart (2021-yildagi o'zgartirishlar). Prod muhitda server mahalliy data-markazda joylashtiriladi.

### Terminal va tarmoq
- Ed25519 imzo quyidagilarni qamrab oladi: `METHOD`, `PATH`, `TIMESTAMP`, `NONCE` va `SHA256(body)`. Server faqat ochiq kalitni saqlaydi.
- Soat farqi 30 soniyadan oshsa yoki nonce qayta ishlatilsa, so'rov rad etiladi (replay himoyasi).
- Rollar ajratilgan: to'lov terminali foydalanuvchini ro'yxatdan o'tkaza olmaydi. Ro'yxatga olish faqat bank filialida, operator pasportni tekshirgandan keyin amalga oshiriladi.
- Rate limit, so'rov hajmi cheklovi, idempotentlik (ikki marta yechib olinmaydi), `SELECT ... FOR UPDATE` bilan balansni qulflash.

### Hujumlar va himoya matritsasi

| Hujum | Himoya | Test |
|---|---|---|
| Chop etilgan foto | Faol sinov (harakat yo'q → rad) | `test_photo_attack_rejected` |
| Telefondagi video | Tasodifiy ketma-ketlik + MiniFASNet | `test_screen_replay_rejected_by_passive` |
| Harakatni o'zi qilib, keyin qurbon rasmini ko'rsatish | Juftlik bo'yicha shaxs izchilligi | `test_face_switch_mid_capture_rejected` |
| Kadrga boshqa odam tushishi | Bir nechta katta yuz bo'lsa rad etiladi | `test_bystander_in_frame_rejected` |
| Egizaklar / juda o'xshash odamlar | Margin qoidasi | `test_lookalikes_rejected_by_margin` |
| Sinovni qayta ishlatish | Bir martalik sinov | `test_challenge_cannot_be_reused` |
| Soxta terminal / so'rovni o'zgartirish | Ed25519 imzo | `test_unsigned_and_forged_requests_rejected` |
| PIN'ni tanlab topish | Bloklash | `test_pin_bruteforce_locks_account` |
| Bir yuzga ikki hisob ochish | Ro'yxatdan o'tishda 1:N tekshiruv | `test_duplicate_face_enrollment_rejected` |
| Bazani o'g'irlash | Shifrlash + shablonni o'zgartirish | `test_no_plaintext_personal_data_in_database` |
| Audit jurnalini o'zgartirish | Xesh-zanjir | `test_audit_chain_detects_tampering` |
| Shablonni "zaharlash" | Kuniga ko'pi bilan 1 ta yangilanish, faqat yuqori ishonchli holatda | `test_template_update_rate_limited` |

## Bank kartasidan to'lov (Uzcard / Humo)

To'lov foydalanuvchining **asosiy bank kartasidan** yechiladi. Karta ulanmagan bo'lsa, FacePay balansidan yechiladi.

- **Karta raqami saqlanmaydi.** Protsessing (Payme) kartani tokenga aylantiradi. Bazada faqat shifrlangan token (AES-GCM, AAD = foydalanuvchi + karta) va `8600 **** **** 9012` ko'rinishidagi niqob turadi.
- **Kartani faqat egasi ulay oladi.** Buning uchun FacePay PIN'i va kartaga bog'langan telefonga kelgan **SMS kod** kerak. 3 marta noto'g'ri kod kiritilsa, karta o'chiriladi.
- **Rollar ajratilgan.** Kartani faqat ro'yxatga olish terminali (bank filiali) orqali ulash mumkin, to'lov terminali karta qo'sha olmaydi.
- **Server yiqilsa ham hisob yo'qolmaydi.** Protsessingga murojaatdan oldin tranzaksiya `charging` holatida saqlanadi. Yiqilish bo'lsa, bu tranzaksiyalar keyin protsessing bilan solishtiriladi (reconciliation).
- **Foydalanuvchi o'chirilsa,** protsessingdagi karta tokenlari ham o'chiriladi.

| Rejim | Qanday yoqiladi |
|---|---|
| Demo (standart) | `FACEPAY_PAYMENT_GATEWAY=mock`. Pul yechilmaydi, SMS kod har doim `666666` |
| Payme | `.env` ga `FACEPAY_PAYMENT_GATEWAY=payme`, `FACEPAY_PAYME_MERCHANT_ID`, `FACEPAY_PAYME_KEY` yoziladi. Test muhit: `checkout.test.paycom.uz` |

Demo kartalar:

| Karta | Natija |
|---|---|
| `8600 1234 5678 9012` (Uzcard) | to'lov o'tadi |
| `9860 1234 5678 9015` (Humo) | to'lov o'tadi |
| `8600 1234 5671 0000` | "kartada mablag' yetarli emas" |

```bash
./facepay card  --phone +998901234567    # karta ulash (raqam, muddat, PIN, SMS kod so'raladi)
./facepay cards --phone +998901234567    # ulangan kartalar
```

> **Prod uchun eslatma:** real tizimda karta raqamini protsessingning o'z formasi yoki SDK si orqali kiritish tavsiya etiladi. Shunda server karta raqamini umuman ko'rmaydi va PCI DSS talablari doirasi kichrayadi.

## Yosh o'zgarishi va makiyaj

**Yosh o'zgarishi** ([app/biometrics/matcher.py](app/biometrics/matcher.py)):
- Moslik 0.60 dan yuqori, passiv liveness 0.8 dan yuqori va kadr sifatli bo'lsa, eng yaqin shablon `0.85·eski + 0.15·yangi` formulasi bilan yangilanadi. Shu tariqa shablon yillar davomida odam bilan birga "qariydi".
- `test_aging_template_adaptation` testi 10 yillik simulyatsiyani tekshiradi. Moslashuvsiz 10 yildan keyingi ball chegaradan past bo'ladi va odam tanilmaydi. Moslashuv bilan ball 0.60 dan yuqori qoladi.
- Oxirgi 20 ta ballning o'rtachasi pasaysa yoki ro'yxatdan o'tganiga 3 yildan oshgan bo'lsa, PIN so'raladi va qayta ro'yxatdan o'tish taklif qilinadi.

**Makiyaj / ko'zoynak / soqol**:
- Ro'yxatdan o'tishda old tomondan va burilgan holatdagi kadrlardan bir nechta shablon olinadi.
- Foydalanuvchi `POST /v1/templates/variant` orqali (PIN + yuz bilan) "makiyaj" shablonini qo'shishi mumkin (`test_heavy_makeup_variant`).
- Ishonchli moslikda yangi ko'rinish (o'xshashlik 0.60–0.80) avtomatik ravishda alohida shablon bo'lib qo'shiladi. Bir odamda ko'pi bilan 6 ta shablon saqlanadi.
- Makiyaj bilan boshqa odamga o'xshashga urinish margin qoidasi bilan to'xtatiladi: kim to'layotgani aniq bo'lmasa, to'lov rad etiladi.

## Internetda ishlaydigan versiya

**https://qudratovagulshoda--facepay-web.modal.run**: brauzer terminali (telefon yoki noutbukda oching). API hujjatlari: `/docs`

[Modal](https://modal.com) serverless platformasida joylashgan. Oyiga $30 bepul kredit beriladi, karta talab qilinmaydi.
- Server faqat so'rov kelganda yonadi va 10 daqiqa tinch turgandan keyin o'chadi. Shuning uchun birinchi ochilish 30–60 soniya davom etishi mumkin, keyingilari tez ishlaydi.
- Baza Modal Volume'da saqlanadi: server o'chib-yonganda ham ma'lumotlar yo'qolmaydi.
- Shifrlash kalitlari Modal Secret'da turadi, kodda yo'q.

Qayta joylash (kod o'zgargandan keyin):

```bash
.venv/bin/modal token new                  # faqat birinchi marta
.venv/bin/python scripts/deploy_modal.py
```

## Muqobil: Hugging Face Spaces (PRO obuna talab qilinadi)

```bash
.venv/bin/python scripts/deploy_hf.py          # Hugging Face token so'raladi
```

Skript quyidagilarni bajaradi:
- Space yaratadi;
- shifrlash kalitlarini yaratib, Space secrets bo'limiga yozadi (kodda ham, repoda ham yo'q);
- kodni yuklaydi.

Taxminan 15 daqiqada `https://<login>-facepay.hf.space` manzilida **brauzer terminali** ishga tushadi. Uni telefon yoki noutbukda ochasiz, kamera brauzerning o'zida ishlaydi, Python kerak emas.

1. **Sozlash** bo'limida admin kalitini bir marta kiritasiz. Brauzer Ed25519 kalit juftini yaratadi. Yopiq kalitni eksport qilib bo'lmaydi, u faqat shu qurilmada qoladi.
2. **Ro'yxatdan o'tish**, keyin **Karta**, keyin **To'lov**.

| Savol | Javob |
|---|---|
| Bepulmi? | Yo'q: Docker Space'lar uchun 2026-yildan PRO obuna kerak |
| Doim ishlaydimi? | 48 soat hech kim kirmasa "uxlaydi". Keyingi kirishda 1–2 daqiqada uyg'onadi |
| Ma'lumotlar saqlanadimi? | Standart holatda SQLite ishlatiladi va Space qayta ishga tushganda tozalanadi. Doimiy saqlash uchun bepul Neon Postgres ulang: `deploy_hf.py --database-url "postgresql://..."` |
| Qonunchilik | Hugging Face serverlari O'zbekistonda emas. Demo uchun faqat test ma'lumotlaridan foydalaning, real foydalanuvchilar uchun mahalliy server kerak |

## O'rnatish va ishga tushirish (lokal)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./scripts/download_models.sh           # MediaPipe va MiniFASNet modellari
python scripts/generate_keys.py        # .env: shifrlash kalitlari + admin kaliti
uvicorn app.main:app --port 8000       # InsightFace buffalo_l birinchi ishga tushishda yuklanadi
```

Demo terminal (noutbuk kamerasi bilan), boshqa terminal oynasida:

```bash
python -m terminal_client.terminal setup --role enroll     # admin kalitini .env dan oling
python -m terminal_client.terminal setup --role payment
python -m terminal_client.terminal enroll --phone +998901234567 --name "Ali Valiyev"
curl -X POST localhost:8000/v1/admin/topup -H "X-Admin-Key: ..." -H "Content-Type: application/json" \
     -d '{"phone": "+998901234567", "amount": 100000}'
python -m terminal_client.terminal pay --amount 1700        # metro: faqat yuz
python -m terminal_client.terminal pay --amount 350000      # katta summa: yuz + PIN
python -m terminal_client.terminal variant --phone +998901234567 --label makiyaj
```

Brauzer terminali: http://localhost:8000 · API hujjatlari: http://localhost:8000/docs

> Agar "chapga buring" sinovi teskari ishlasa (kamera ko'zgu tasvir berayotgan bo'lsa), `.env` fayliga `FACEPAY_YAW_SIGN=-1` qo'shing.

## Testlar

```bash
pytest -q        # 56 ta test
```

Testlar neyron tarmoqsiz ishlaydi: yuz analizatori soxta (mock) bilan almashtiriladi. Shu tufayli xavfsizlik mantiqining hammasini (imzo, liveness, 1:N, PIN, limitlar, shifrlash) tez va takrorlanadigan tarzda sinash mumkin.

Aniqlikni haqiqiy suratlarda baholash (diplomning "Tajriba natijalari" bo'limi uchun):

```bash
python scripts/evaluate.py path/to/LFW --out lfw.csv      # FAR, FRR, EER, 1:N natijalari
python scripts/evaluate.py path/to/FG-NET --out age.csv   # yosh o'zgarishi
```

## Loyiha tuzilishi

```
app/
  config.py                  chegaralar, limitlar, kalitlar (muhit o'zgaruvchilaridan)
  db.py                      modellar (hammasi shifrlangan)
  core.py                    kripto/galereya obyektlari, audit xesh-zanjiri
  main.py                    API endpointlari
  security/
    crypto.py                AES-256-GCM, HMAC, scrypt
    template_protection.py   bekor qilinadigan biometriya
    terminal_auth.py         Ed25519 imzo, nonce, rate limit
  biometrics/
    analyzer.py              InsightFace + MediaPipe
    quality.py               kadr sifati, asosiy yuzni tanlash
    liveness.py              faol + passiv liveness
    pipeline.py              kadrlar → tekshiruvlar → embedding
    matcher.py               1:N, margin, yoshga moslashish
  gateway.py                 karta protsessingi: Mock va Payme
  services/
    users.py                 ro'yxatdan o'tish, variant, o'chirish
    cards.py                 karta ulash, SMS tasdiqlash, o'chirish
    payments.py              to'lov, PIN, limitlar
terminal_client/             demo terminal (kamera + imzo)
scripts/                     kalitlar, modellar, baholash
tests/                       56 ta test
```

## Cheklovlar va keyingi ishlar (diplomda ochiq yozish tavsiya etiladi)

- **3D silikon niqob:** oddiy RGB kamera bilan to'liq aniqlab bo'lmaydi. Real turniketlarda IQ (infraqizil) yoki chuqurlik kamerasi (masalan, Intel RealSense) qo'shiladi.
- **Chegaralarni kalibrlash:** 0.45 / 0.60 / 0.08 qiymatlari ArcFace uchun odatiy qiymatlar. Ularni mahalliy (O'zbekiston aholisi) ma'lumotlari asosida `scripts/evaluate.py` bilan qayta kalibrlash kerak.
- **Masshtab:** galereya RAM'da saqlanadi (numpy). Millionlab foydalanuvchi uchun FAISS/HNSW indeks, PostgreSQL, Redis (nonce va rate limit) kerak bo'ladi.
- **Kalitlarni boshqarish:** master kalit `.env` da turibdi. Prod muhitda HSM yoki KMS ishlatiladi, kalitlar muntazam almashtiriladi (versiya maydoni tayyor).
- **Passiv liveness:** MiniFASNetV2 ochiq ma'lumotlarda o'qitilgan. Real kamera va yoritish sharoitida qo'shimcha o'qitish (fine-tuning) aniqlikni oshiradi. Model yo'q bo'lsa ishlatiladigan evristika zaif, uni faqat demo uchun ishlatish mumkin.
- **PIN tiklash uchun SMS:** hozir PIN faqat yuz orqali tiklanadi (liveness + 1:N margin). Real tizimda qo'shimcha ravishda ro'yxatdagi telefonga SMS kod yuboriladi (SMS provayder: Eskiz, Play Mobile).
- **Karta protsessingi:** Payme integratsiyasi yozilgan, lekin haqiqiy merchant kaliti bilan sinalmagan. Metod nomlari va maydonlarni Payme hujjatlari bilan solishtirish kerak.

## Litsenziyalar
- InsightFace modellari (buffalo_l) faqat **notijorat / ilmiy** maqsadda ishlatilishi mumkin. Diplom uchun mos, tijorat uchun alohida litsenziya olinadi.
- MiniFASNet (Minivision): Apache-2.0. MediaPipe: Apache-2.0.
