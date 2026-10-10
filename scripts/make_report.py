"""Skrinshotlar va izohlardan bitta hujjat yasaydi: docs/hisobot.html va docs/hisobot.pdf.

    .venv/bin/python scripts/make_screenshots.py     # avval skrinshotlar
    .venv/bin/python scripts/make_report.py          # keyin hujjat
"""
from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "screens"
OUT_HTML = ROOT / "docs" / "hisobot.html"
OUT_PDF = ROOT / "docs" / "hisobot.pdf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

TITLE = "FacePay — yuz orqali to'lov tizimi"
SUBTITLE = "Tizimning ishlash bosqichlari: ekran ko'rinishlari va izohlar"

# (fayl, sarlavha, izoh paragraflari)
STEPS: list[tuple[str | None, str, list[str]]] = [
    (None, "1. Tizim haqida qisqacha", [
        "FacePay — foydalanuvchi karta yoki telefonsiz, faqat yuzi orqali to'lov qiladigan tizim. "
        "Yuz tasviri saqlanmaydi: undan 512 o'lchamli matematik vektor (shablon) olinadi, u maxfiy "
        "matritsa bilan o'zgartiriladi va AES-256-GCM bilan shifrlangan holda saqlanadi.",
        "Tizim uchta ish rejimida ishlaydi: <b>mijoz kabineti</b> (foydalanuvchi o'z telefonida ro'yxatdan "
        "o'tadi va kartasini ulaydi), <b>kassa + mijoz ekrani</b> (sotuvchi summani kiritadi, mijoz yuzi bilan "
        "tasdiqlaydi) va <b>kassirsiz tezkor terminal</b> (qat'iy narx, bitta tugma).",
        "Texnologiyalar: Python (FastAPI), InsightFace (SCRFD yuz detektori + ArcFace tanish modeli), "
        "MediaPipe (bosh holati, ko'z va og'iz harakati), MiniFASNetV2 (tirikligini tekshirish), "
        "SQLAlchemy, Ed25519 raqamli imzo. Server Modal bulutida joylashgan, kod GitHub'da.",
    ]),
    (None, "2. To'lovning umumiy sxemasi", [
        "<pre class=\"diagram\">"
        "  MIJOZ EKRANI (kamera)            SERVER                         KASSA (sotuvchi)\n"
        "         |                            |                                 |\n"
        "         |                            |&lt;---- 1. summa: 67 000 so'm -----|\n"
        "         |&lt;--- 2. to'lov so'rovi -----|                                 |\n"
        "         |                            |                                 |\n"
        "   3. tasodifiy sinov:                |                                 |\n"
        "   \"ko'zingizni qising\"               |                                 |\n"
        "         |---- 4. 14 ta kadr -------&gt;|                                 |\n"
        "         |                      5. tekshiruvlar:                        |\n"
        "         |                       - bitta yuzmi                          |\n"
        "         |                       - harakat bajarildimi (tiriklik)       |\n"
        "         |                       - rasm/ekran emasmi (passiv model)     |\n"
        "         |                       - barcha kadrda bir odammi             |\n"
        "         |                       - 1:N qidiruv (kim ekanligi)           |\n"
        "         |&lt;--- 6. natija ------------|---- 6. natija ----------------&gt;|\n"
        "</pre>",
        "Muhim xavfsizlik qoidasi: <b>summani faqat kassa belgilaydi</b>. Mijoz ekrani serverga faqat "
        "so'rov raqamini yuboradi, summani o'zgartira olmaydi.",
    ]),

    ("01-kabinet-royxat.png", "3. Mijoz kabineti: ro'yxatdan o'tish", [
        "Foydalanuvchi o'z telefonida saytni ochadi. Telefon raqami, ismi va o'zi o'ylab topgan 4–6 xonali "
        "PIN kiritiladi. <code>1234</code>, <code>1111</code> kabi oddiy PIN'lar qabul qilinmaydi.",
        "Rozilik belgisi majburiy: \"Shaxsga doir ma'lumotlar to'g'risida\"gi Qonun (O'RQ-547) biometrik "
        "ma'lumotni faqat aniq rozilik bilan qayta ishlashga ruxsat beradi. Matnda rasm saqlanmasligi va "
        "ma'lumotlarni istalgan vaqtda o'chirish mumkinligi ko'rsatilgan.",
    ]),
    ("02-kabinet-kamera.png", "4. Tiriklikni tekshirish (liveness)", [
        "Kamera yoqiladi va server <b>tasodifiy</b> tanlagan bitta harakat so'raladi: ko'zni qisish, og'izni "
        "ochish yoki boshni chapga/o'ngga burish. Harakat server tomonida tanlangani uchun oldindan yozib "
        "olingan videoni ko'rsatib aldab bo'lmaydi.",
        "~3,4 soniyada 14 ta kadr olinadi. Kadrlar faqat harakat paytida zich olinadi, chunki ko'z qisish "
        "150–300 millisekund davom etadi va siyrak suratga olishda e'tibordan chetda qolishi mumkin.",
        "Kadrlar WebP formatida (480 piksel) yuboriladi — bu JPEG'dan uch barobar kichik, tanish aniqligiga "
        "ta'siri esa sezilmaydi (o'xshashlik 0,98+). Sekin internetda ham tez ishlashi uchun shunday qilingan.",
    ]),
    ("03-kabinet-royxat-natija.png", "5. Ro'yxatdan o'tish yakunlandi", [
        "Server kadrlardan eng sifatlilarini tanlab, yuz shablonini yaratadi va shifrlab saqlaydi. "
        "Kadrlarning o'zi diskka ham, jurnalga ham yozilmaydi.",
        "Shu bosqichda <b>takroriy ro'yxatdan o'tish</b> ham tekshiriladi: agar bu yuz allaqachon boshqa "
        "hisobga bog'langan bo'lsa, yangi hisob ochilmaydi.",
    ]),
    ("04-kabinet-karta.png", "6. Bank kartasini ulash", [
        "Mijoz Uzcard yoki Humo kartasini ulaydi. <b>Karta raqami serverda saqlanmaydi</b>: protsessing "
        "(Payme) kartani tokenga aylantiradi, bazada faqat shifrlangan token va niqoblangan raqam qoladi.",
        "Kartani ulash uchun FacePay PIN'i talab qilinadi — ya'ni buni faqat hisob egasi qila oladi.",
    ]),
    ("05-kabinet-karta-sms.png", "7. Kartani SMS kod bilan tasdiqlash", [
        "Kartaga bankda bog'langan telefon raqamiga SMS kod yuboriladi. Bu — kartaning haqiqatan shu odamga "
        "tegishli ekanini tasdiqlaydi, ya'ni birovning kartasini ulab bo'lmaydi.",
        "Kod 3 marta xato kiritilsa, karta o'chiriladi va qaytadan ulash kerak bo'ladi. "
        "Skrinshotda demo rejim ko'rsatilgan: haqiqiy SMS yuborilmaydi, kod har doim 666666.",
    ]),
    ("06-kabinet-kartalar.png", "8. Kartalarni boshqarish", [
        "Mijoz uchtagacha karta ulashi, asosiysini tanlashi va keraksizini o'chirishi mumkin. "
        "To'lovlar asosiy kartadan yechiladi.",
        "Karta o'chirilganda protsessingdagi token ham bekor qilinadi.",
    ]),
    ("07-kabinet-tarix.png", "9. To'lovlar tarixi", [
        "Mijoz o'z to'lovlarini ko'radi: qayerda, qachon, qancha va qaysi kartadan. "
        "Ro'yxatda <b>rad etilgan urinishlar ham</b> ko'rsatiladi — agar kimdir uning yuzi bilan to'lashga "
        "uringan bo'lsa, foydalanuvchi buni darhol sezadi.",
        "Tarixni ko'rish uchun PIN talab qilinadi.",
    ]),
    ("08-kabinet-sozlamalar.png", "10. PIN tiklash, makiyaj shabloni, ma'lumotlarni o'chirish", [
        "<b>PIN esdan chiqsa</b>, uni yuz orqali tiklash mumkin: shaxs 1:N qidiruv bilan tasdiqlanadi va "
        "yangi PIN o'rnatiladi. Buning uchun yuz hisob egasiga ishonchli mos kelishi hamda butun bazada "
        "eng yaqin odam aynan shu foydalanuvchi bo'lishi shart.",
        "<b>Makiyaj, ko'zoynak yoki soqol</b> bilan tanilish pasaysa, foydalanuvchi shu ko'rinishda "
        "qo'shimcha shablon qo'shadi. Har bir foydalanuvchida oltitagacha shablon saqlanadi.",
        "<b>Unutilish huquqi:</b> bitta tugma bilan yuz shablonlari, kartalar, ism va telefon butunlay "
        "o'chiriladi (PIN va yuz bilan tasdiqlanadi).",
    ]),

    ("08b-kabinet-pauza.png", "10a. Yuz orqali to'lovni vaqtincha to'xtatish va ma'lumotlarni yuklab olish", [
        "Telefon yo'qolsa yoki shubhali to'lov ko'rinsa, foydalanuvchi yuz orqali to'lovni bitta tugma bilan "
        "o'chirib qo'yadi. Shablonlar saqlanib qoladi — keyin qayta yoqish mumkin. Bu GDPR va BIPA talab "
        "qiladigan \"rozilikni istalgan vaqtda qaytarib olish\" huquqining amaliy ko'rinishi.",
        "Shu bo'limda foydalanuvchi o'zida saqlanayotgan barcha ma'lumotlarni (ism, telefon, kartalar, "
        "to'lovlar, shablonlar ro'yxati) fayl sifatida yuklab oladi — GDPR 20-moddasidagi ma'lumotlarni "
        "ko'chirish huquqi. Yuz shablonining o'zi berilmaydi, chunki u shifrlangan va boshqa tizimda "
        "ishlatib bo'lmaydi.",
    ]),
    ("09-ekran-sozlash.png", "11. Do'kondagi mijoz ekranini sozlash", [
        "Mijozga qaratilgan kamerali qurilma (planshet yoki telefon) bir marta ro'yxatdan o'tkaziladi. "
        "Qurilma brauzerning o'zida Ed25519 kalit juftligini yaratadi; yopiq kalitni eksport qilib bo'lmaydi "
        "va u faqat shu qurilmada qoladi. Server esa faqat ochiq kalitni saqlaydi.",
        "Shundan keyin qurilmaning har bir so'rovi raqamli imzo bilan yuboriladi — soxta terminal nomidan "
        "so'rov yuborib bo'lmaydi.",
    ]),
    ("10-ekran-ulash-kodi.png", "12. Kassani ulash kodi", [
        "Mijoz ekrani 6 xonali bir martalik kod ko'rsatadi (10 daqiqa amal qiladi). Bazada kodning o'zi emas, "
        "faqat uning HMAC xeshi saqlanadi.",
        "Bu kod kassa qurilmasini shu mijoz ekraniga bog'laydi. Ulash bir marta bajariladi — har bir mijoz "
        "uchun qayta ulash kerak emas.",
    ]),
    ("16-kassa-ulash.png", "13. Kassa tomoni: ulanish", [
        "Sotuvchi kassasi — kamerasiz qurilma. U mijoz ekranida chiqqan kodni kiritadi va o'zining alohida "
        "kalitini yaratadi.",
        "Rollar ajratilgan: kassa to'lovni o'zi bajara olmaydi (kamerasi yo'q), mijoz ekrani esa to'lov "
        "so'rovi yarata olmaydi (summani belgilay olmaydi).",
    ]),
    ("11-ekran-kutish.png", "14. Kutish holati", [
        "Mijoz ekrani bo'sh turganda \"Xush kelibsiz\" yozuvini ko'rsatadi va har 2 soniyada yangi to'lov "
        "so'rovi bor-yo'qligini tekshiradi.",
    ]),
    ("17-kassa-kutilmoqda.png", "15. Kassir summani kiritadi", [
        "Sotuvchi summani kiritib, \"To'lovni so'rash\" tugmasini bosadi. Server to'lov so'rovini yaratadi "
        "(3 daqiqa amal qiladi) va uni mijoz ekraniga yuboradi.",
        "Kassa ekranida holat jonli ko'rinadi: kutilmoqda → yuz tekshirilmoqda → to'landi yoki rad etildi. "
        "Sotuvchi istalgan paytda bekor qila oladi.",
    ]),
    ("12-ekran-tolov.png", "16. Mijoz summani ko'radi va yuzini ko'rsatadi", [
        "Mijoz ekranida summa katta harflar bilan chiqadi va kamera <b>o'zi</b> ishga tushadi — mijoz hech "
        "qanday tugma bosmaydi. Ekranda bajariladigan harakat yoziladi.",
        "Agar yuz tanilmasa yoki harakat bajarilmasa, tizim uch martagacha avtomatik qayta uriniadi, "
        "keyin \"Kassirga murojaat qiling\" deb yozadi. To'lov so'rovi yo'qolmaydi.",
    ]),
    ("13-ekran-pin.png", "17. Katta summa: PIN mijozning o'z ekranida", [
        "Summa 200 000 so'mdan oshsa yoki tanish ishonchi past bo'lsa, PIN so'raladi. PIN <b>mijoz ekranida</b> "
        "kiritiladi — kassir uni ko'rmaydi.",
        "Ekranda mijozning to'liq ismi emas, niqoblangan ko'rinishi chiqadi (masalan \"G****** Q.\").",
    ]),
    ("14-ekran-tolandi.png", "18. To'lov muvaffaqiyatli", [
        "Mijoz ekranida natija va qaysi kartadan yechilgani ko'rsatiladi. Olti soniyadan keyin ekran "
        "kutish holatiga qaytadi.",
        "Shu paytda server, agar tanish ishonchi yuqori bo'lsa, yuz shablonini asta-sekin yangilaydi — "
        "shu tariqa shablon yillar davomida odam bilan birga \"qariydi\".",
    ]),
    ("18-kassa-tolandi.png", "19. Kassada natija va kunlik tushum", [
        "Kassa ekranida to'lov tasdiqlangani, kimligi (niqoblangan ism) va usuli (yuz yoki yuz + PIN) "
        "ko'rinadi. Pastda bugungi tushum va oxirgi tranzaksiyalar ro'yxati.",
        "Bu ro'yxatda mijozlarning shaxsiy ma'lumotlari ko'rsatilmaydi.",
    ]),
    ("15-ekran-rad.png", "20. Hujum aniqlanganda", [
        "Skrinshotda tizim fotosurat yoki ekrandagi tasvirni aniqlab, to'lovni rad etgani ko'rsatilgan.",
        "Tizim quyidagi hujumlarni to'xtatadi: chop etilgan foto, telefondagi video, oldindan yozilgan video, "
        "kadrlar orasida yuzni almashtirish, kadrga begona odamning tushishi, bir-biriga juda o'xshash "
        "odamlar (bunday holatda to'lov rad etiladi yoki PIN so'raladi).",
    ]),

    ("19-terminal-sozlash.png", "21. Kassirsiz tezkor terminal: sozlash", [
        "Kassirsiz rejim qat'iy narxli joylar uchun: kirish nazorati, avtomat, belgilangan narxli xizmat.",
        "Narx <b>serverda</b> saqlanadi. Qurilma buzib olinsa va boshqa summa yuborsa ham, server o'z narxini "
        "qo'llaydi — bu alohida test bilan tekshirilgan.",
    ]),
    ("20-terminal-tolandi.png", "22. Kassirsiz terminalda to'lov", [
        "Mijoz bitta tugmani bosadi va natija katta harflar bilan chiqadi. Pastda kunlik hisob yuritiladi.",
        "Bu qurilmada klaviatura yo'q, shuning uchun PIN talab qilinadigan summada mijoz kassaga "
        "yo'naltiriladi.",
    ]),
    ("21-admin-panel.png", "23. Administrator paneli", [
        "Tizim holati: foydalanuvchilar va shablonlar soni, ulangan kartalar, bugungi to'lovlar va tushum, "
        "rad etish sabablari reytingi, terminallar ro'yxati.",
        "<b>Audit jurnali</b> alohida ahamiyatga ega: har bir yozuv o'zidan oldingisining SHA-256 xeshini "
        "saqlaydi. Agar kimdir jurnalni o'zgartirsa, zanjir buziladi va panel buni ko'rsatadi. "
        "Jurnalda shaxsiy ma'lumot va biometrik ma'lumot saqlanmaydi.",
    ]),
]

SECURITY_ROWS = [
    ("Chop etilgan foto", "Faol sinov: harakat bajarilmaydi"),
    ("Telefon yoki planshetdagi video", "Tasodifiy sinov + passiv model (MiniFASNetV2)"),
    ("Oldindan yozib olingan video", "Sinovni server tanlaydi, 30 soniya amal qiladi, bir martalik"),
    ("Harakatni o'zi qilib, keyin qurbon rasmini ko'rsatish", "Har bir juft kadrda bir xil shaxs ekani tekshiriladi"),
    ("Kadrga begona odam tushishi", "Ikkinchi yuz katta bo'lsa, to'lov rad etiladi"),
    ("Egizaklar yoki juda o'xshash odamlar", "1-o'rin va 2-o'rin orasidagi farq (margin) talab qilinadi"),
    ("Soxta terminal yoki so'rovni o'zgartirish", "Ed25519 imzo, vaqt tamg'asi va bir martalik nonce"),
    ("Bazani o'g'irlash", "Shablon maxfiy matritsa bilan o'zgartirilgan va AES-256-GCM bilan shifrlangan"),
    ("PIN'ni tanlab topish", "Urinishlar cheklangan va bloklash; blok yuz bilan kichik to'lovga ta'sir qilmaydi"),
    ("Audit jurnalini o'zgartirish", "Xesh-zanjir: o'zgartirish darhol seziladi"),
]

RESULTS_ROWS = [
    ("Teng xatolik nuqtasi (EER)", "1,71%"),
    ("FAR — begonani qabul qilish (chegara 0,50)", "0,000% (20 000 juftlikdan 0 ta)"),
    ("FRR — o'zini rad etish", "6,5%"),
    ("1:N rejimda PIN'siz xato to'lov", "0,14% (chegaralar o'lchov asosida tanlangandan keyin)"),
    ("Bitta shablon bilan to'g'ri tanish", "94,3%"),
    ("Uchta shablon bilan to'g'ri tanish", "96,7%"),
]


def img_tag(name: str) -> str:
    data = base64.b64encode((SHOTS / name).read_bytes()).decode()
    return f'<img src="data:image/png;base64,{data}" alt="{name}">'


def build_html() -> str:
    parts = []
    for img, title, paragraphs in STEPS:
        body = "".join(f"<p>{p}</p>" for p in paragraphs)
        picture = f'<figure>{img_tag(img)}</figure>' if img else ""
        parts.append(f'<section><h2>{title}</h2>{picture}{body}</section>')

    sec_rows = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in SECURITY_ROWS)
    res_rows = "".join(f"<tr><td>{a}</td><td class='num'>{b}</td></tr>" for a, b in RESULTS_ROWS)
    chart = ""
    if (ROOT / "docs" / "natijalar_lfw.png").exists():
        data = base64.b64encode((ROOT / "docs" / "natijalar_lfw.png").read_bytes()).decode()
        chart = f'<figure class="wide"><img src="data:image/png;base64,{data}" alt="FAR/FRR">' \
                f'<figcaption>FAR va FRR egri chiziqlari hamda o\'xshashliklar taqsimoti (LFW)</figcaption></figure>'

    return f"""<!doctype html>
<html lang="uz"><head><meta charset="utf-8"><title>{TITLE}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{ font: 11.5pt/1.5 "Times New Roman", Georgia, serif; color: #111; max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 20pt; margin: 0 0 4px; }}
  .sub {{ color: #444; font-size: 12pt; margin: 0 0 6px; }}
  .meta {{ color: #666; font-size: 10pt; margin-bottom: 22px; }}
  h2 {{ font-size: 13pt; margin: 22px 0 8px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
  section {{ page-break-inside: avoid; }}
  p {{ margin: 6px 0; text-align: justify; }}
  figure {{ margin: 10px 0 12px; text-align: center; }}
  figure img {{ max-width: 78%; border: 1px solid #ddd; border-radius: 6px; }}
  figure.wide img {{ max-width: 100%; }}
  figcaption {{ color: #666; font-size: 9.5pt; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 10.5pt; margin: 8px 0 14px; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f2f4; }}
  td.num {{ white-space: nowrap; }}
  code {{ font-family: ui-monospace, Menlo, monospace; font-size: 10pt; background: #f4f5f7; padding: 0 3px; }}
  pre.diagram {{ font-family: ui-monospace, Menlo, monospace; font-size: 8.5pt; line-height: 1.35;
                 background: #f7f8fa; border: 1px solid #e2e5ea; border-radius: 6px; padding: 10px; overflow: hidden; }}
  ul {{ margin: 6px 0 6px 18px; }}
</style></head><body>
<h1>{TITLE}</h1>
<p class="sub">{SUBTITLE}</p>
<p class="meta">Bitiruv malakaviy ishi · Muallif: Gulshoda Qudratova · Sana: {time.strftime('%d.%m.%Y')}<br>
Ishlab turgan versiya: https://qudratovagulshoda--facepay-web.modal.run · Kod: https://github.com/QudratovaGulshoda/Facepayment</p>

{''.join(parts)}

<section><h2>25. Hujumlar va himoya choralari</h2>
<table><tr><th>Hujum</th><th>Himoya</th></tr>{sec_rows}</table>
<p>Har bir qator avtomatlashtirilgan test bilan tekshiriladi. Loyihada jami 89 ta test bor.</p>
</section>

<section><h2>24. Tajriba natijalari</h2>
<p>Aniqlik ochiq LFW (Labeled Faces in the Wild) bazasida o'lchandi: 1680 shaxs, 3294 surat,
4381 ta haqiqiy va 20 000 ta soxta juftlik.</p>
<table><tr><th>Ko'rsatkich</th><th>Natija</th></tr>{res_rows}</table>
{chart}
<p>Chegara qiymatlari (0,50 / 0,16 / 0,70) aynan shu o'lchov asosida tanlandi: boshlang'ich qiymatlarda
PIN'siz xato to'lov 0,72% edi, tanlangan qiymatlarda 0,14% ga tushdi. Ko'p shablonli yondashuvning
foydasi ham o'lchov bilan tasdiqlandi.</p>
<p>LFW — internetdan olingan, turli yoritish va burchakdagi suratlar to'plami. Terminal kamerasi yaqindan,
yorug' joyda suratga oladi va tizim bir nechta kadrni o'rtachalaydi, shuning uchun real sharoitda xatolik
bundan kam bo'ladi.</p>
</section>

<section><h2>26. Chet eldagi tizimlar bilan taqqoslash</h2>
<p>Loyiha quyidagi ishlayotgan tizimlar tajribasi va xalqaro talablar asosida to'ldirildi.</p>
<table>
<tr><th>Tizim</th><th>Kuchli tomoni</th><th>Muammosi</th></tr>
<tr><td>Alipay Smile to Pay (Xitoy, 2017)</td><td>3D kamera va liveness; keng tarqalgan</td>
    <td>Ishonch muammosi: 2017-yilgi so'rovda respondentlarning ~70% i xavfsizlikdan, 77% i maxfiylikdan xavotirda bo'lgan</td></tr>
<tr><td>Moscow Metro Face Pay (2021)</td><td>1500+ turniket, 100 mln+ o'tish; sinovda xato ~0,01%</td>
    <td>2022-yilda ba'zi foydalanuvchilardan pul bir necha marta yechilgan; kuzatuv bo'yicha tanqid</td></tr>
<tr><td>Mastercard Biometric Checkout (2022)</td><td>Standart va sertifikatlash talabi; yuz tasviri qurilmada qoladi</td>
    <td>Texnik talablari ochiq e'lon qilinmagan</td></tr>
<tr><td>NIST SP 800-63A, FIDO, ISO/IEC 30107</td><td>Aniq talablar: majburiy rozilik, liveness sinovi (PAD), IAPAR chegarasi</td>
    <td>Sertifikatlash laboratoriya sinovini talab qiladi</td></tr>
</table>
<p><b>Ulardan o'rganib qo'shilgan imkoniyatlar:</b></p>
<table>
<tr><th>Muammo</th><th>Loyihadagi yechim</th></tr>
<tr><td>Bir to'lov uchun bir necha marta pul yechilishi (Moskva, 2022)</td>
    <td>Bir xil terminalda bir xil summa 60 soniya ichida takrorlansa, to'lov rad etiladi</td></tr>
<tr><td>Rozilikni qaytarib olish (GDPR, BIPA)</td>
    <td>Yuz orqali to'lovni vaqtincha o'chirish va qayta yoqish; shablonlar saqlanadi</td></tr>
<tr><td>Begona to'lovga e'tiroz</td>
    <td>Tarixda "Bu men emasman": tranzaksiya nizoli deb belgilanadi va yuz to'lovlari darhol to'xtatiladi</td></tr>
<tr><td>Tovar qaytarilishi</td><td>Kassadan to'liq yoki qisman qaytarish; pul kartaga yoki balansga qaytadi</td></tr>
<tr><td>Ma'lumotlarni ko'chirish huquqi (GDPR 20-modda)</td><td>Barcha ma'lumotlarni JSON fayl sifatida yuklab olish</td></tr>
<tr><td>Biometrik ma'lumotni muddatsiz saqlash (BIPA: 3 yil)</td>
    <td>Saqlash muddati siyosati: 3 yil faolliksiz hisobning shablonlari avtomatik o'chiriladi</td></tr>
<tr><td>Rozilik matni keyin o'zgarishi</td><td>Rozilik versiyasi va sanasi saqlanadi, eksport faylida ko'rinadi</td></tr>
</table>
<p><b>Nimada ulardan oldinda:</b> ko'pchilik tizimlarda yuz shabloni shifrlangan bo'lsa-da, asl vektor ko'rinishida
saqlanadi. Bu loyihada shablon avval maxfiy ortogonal matritsa bilan o'zgartiriladi (bekor qilinadigan biometriya):
kalit almashtirilsa, o'g'irlangan shablonlar butunlay yaroqsiz bo'ladi.</p>
<p><b>Nimada ortda:</b> ularda 3D yoki infraqizil kameralar, laboratoriya sertifikati (ISO/IEC 30107-3) va
millionlab foydalanuvchida sinovdan o'tgan tajriba bor.</p>
</section>

<section><h2>27. Cheklovlar va keyingi ishlar</h2>
<ul>
<li>Uch o'lchovli silikon niqobni oddiy kamera to'liq aniqlay olmaydi — infraqizil yoki chuqurlik kamerasi kerak.</li>
<li>Chegaralar LFW da o'lchandi; O'zbekiston aholisi suratlarida qayta o'lchash aniqlikni oshiradi.</li>
<li>Karta protsessingi (Payme) integratsiyasi yozilgan, lekin haqiqiy merchant shartnomasisiz sinalmagan —
hozircha demo rejimda ishlaydi va haqiqiy pul yechilmaydi.</li>
<li>PIN tiklashda qo'shimcha SMS tasdiqlash qo'shilishi kerak.</li>
<li>Millionlab foydalanuvchi uchun qidiruvni FAISS/HNSW indeksiga o'tkazish talab qilinadi.</li>
<li>Liveness ISO/IEC 30107-3 bo'yicha mustaqil laboratoriyada sinovdan o'tkazilmagan;
NIST SP 800-63A talab qiladigan IAPAR ko'rsatkichi o'lchanmagan.</li>
<li>Qonunchilik talabi: fuqarolarning shaxsiy ma'lumotlari O'zbekiston hududidagi serverlarda saqlanishi shart.
Hozirgi demo chet eldagi bulutda, shuning uchun unda faqat sinov ma'lumotlari ishlatiladi.</li>
</ul>
</section>
</body></html>"""


def main() -> None:
    OUT_HTML.write_text(build_html())
    print(f"HTML: {OUT_HTML} ({OUT_HTML.stat().st_size / 1024 / 1024:.1f} MB)")
    profile = ROOT / ".tmp_screens" / "profile-pdf"
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-pdf-header-footer", "--virtual-time-budget=8000",
                             f"--print-to-pdf={OUT_PDF}", f"file://{OUT_HTML}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        if OUT_PDF.exists() and proc.poll() is None:
            time.sleep(1.0)
            break
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        proc.terminate()
    import shutil

    shutil.rmtree(ROOT / ".tmp_screens", ignore_errors=True)
    if OUT_PDF.exists():
        print(f"PDF:  {OUT_PDF} ({OUT_PDF.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print("PDF yaratilmadi")


if __name__ == "__main__":
    main()
