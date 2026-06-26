# 🗺️ أداة سحب بيانات Google Maps

أداة بايثون تسحب بيانات الأماكن من Google Maps وتُصدّرها إلى ملف **JSON** (و CSV اختيارياً)، عبر متصفح آلي (Playwright).

## ⚠️ تنبيه قانوني
الكشط الآلي لـ Google Maps **قد يخالف شروط خدمة Google**. استخدم الأداة لأغراض البحث الشخصي والتعليمي فقط، وتحمّل مسؤولية استخدامك. للاستخدام التجاري استخدم [Google Places API](https://developers.google.com/maps/documentation/places/web-service) الرسمي.

---

## 📦 التثبيت

شغّل ملف التثبيت (سيُثبّت المكتبات + متصفح Chromium):

```bat
install.bat
```

أو يدوياً:

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## 🚀 الاستخدام

### 🌐 واجهة الويب (الأسهل والأغنى)

```bat
start_web.bat
```
أو يدوياً:
```bash
python app.py
```
ثم افتح المتصفح على: **http://localhost:5000** — بحث، فلاتر، فرز، وتصدير JSON/CSV من المتصفح.

### قائمة أوامر تفاعلية (CLI)

```bat
run.bat
```

### من سطر الأوامر

**1) بحث بكلمة ومدينة:**
```bash
python run.py search --keyword "مطاعم" --city "القاهرة" --max 30
```

**2) رابط موقع محدد:**
```bash
python run.py url --url "https://www.google.com/maps/place/..."
```

**3) قائمة من ملف** (`input_list.txt` — سطر لكل رابط أو اسم):
```bash
python run.py file --input input_list.txt
```

---

## ⚙️ الخيارات

| الخيار | الوصف |
|---|---|
| `--no-headless` | افتح المتصفح بشكل مرئي لتتبع العملية |
| `--lang ar` | لغة الواجهة (ar / en / fr ...) |
| `--output path.json` | مسار ملف JSON الناتج |
| `--csv path.csv` | احفظ نسخة CSV إضافية |
| `--proxy "server=host:port;username=u;password=p"` | بروكسي اختياري |
| `--verbose` | سجلات تفصيلية |

---

## 💼 حزمة الأعمال — من البيانات إلى عميل يدفع

بعد السحب، توجد أدوات تحوّل البيانات إلى حملة بيع كاملة:

| الأداة | الوظيفة | المخرجات |
|---|---|---|
| `python leads.py` | **محرّك العملاء**: يرتّب الفرص (بلا موقع + تقييم) ويولّد روابط واتساب عرض | `output/leads_*.csv` + `.html` |
| `python outreach.py` | **حملة التواصل**: لكل منشأة صفحة حجز + مقترح احترافي بالأرقام + رسالة جاهزة | `output/outreach/` |
| `python booking_system.py` | **نظام الحجز الكامل**: تقويم مواعيد + لوحة إدارة (PIN) + **لوحة مالك** (اشتراكات/إيراد) + إشعارات | `import` / `run` (منفذ 5001) |
| `python pipeline.py` | **يربط كل شيء بأمر واحد**: سحب → فرز → حِزَم عرض → (اختياري) استيراد للحجز | الكل أعلاه |
| `python send_campaign.py` | **إرسال واتساب تلقائي** للرسائل التسويقية عبر WaSenderAPI (تجريبي افتراضياً) | رسائل + سجل `output/.campaign_sent.json` |
| `python publish.py` | **نشر صفحات الهبوط** على GitHub Pages (روابط عامة جاهزة للإرسال) | `site/` + `output/publish_links.csv` |

### 🌐 نشر صفحات الهبوط (GitHub Pages)
يتطلّب `gh` مُسجّلاً (`gh auth login`). ينشر صفحات الحجز فقط بمعرّفات مبهمة + `noindex` (خصوصية).
```bash
python publish.py deploy --repo booking-demos
# → https://USER.github.io/booking-demos/<id>/  لكل منشأة (في output/publish_links.csv)
```
ثم أرسِل الحملة بالروابط الحيّة تلقائياً:
```bash
python send_campaign.py --links-file output/publish_links.csv --send
```
لحذف الاستضافة: `gh repo delete USER/booking-demos --yes`

**نطاق مخصّص** بدل رابط github.io:
```bash
python publish.py deploy --repo booking-demos --domain booking.example.com
```
يكتب ملف `CNAME` ويضبط النطاق، ثم يطبع **سجلات DNS** المطلوبة (CNAME للنطاق الفرعي أو A للجذر). أضِفها لدى مزوّد نطاقك ثم فعّل HTTPS.

### ▲ النشر على Vercel (صفحات الديمو الثابتة)
صفحات الهبوط ثابتة فترفع على Vercel مباشرةً (يتطلّب `vercel login`):
```bash
python publish.py build --site booking-site --base-url https://<مشروعك>.vercel.app/
vercel deploy booking-site --prod --yes --scope <نطاق-حسابك>
```
موقع حيّ حالياً: **https://booking-site-azure.vercel.app** (الصفحة لكل منشأة: `/<id>/`).
> ⚠️ هذا للصفحات الثابتة فقط. **نظام الحجز الديناميكي (Flask+SQLite) لا يعمل على Vercel** (قرص مؤقت لا يحفظ القاعدة) — استضِفه على خادم دائم (Render/Railway) أو بقاعدة سحابية.

### 🟣 نشر نظام الحجز الديناميكي على Render
الكود جاهز (`wsgi.py` + `render.yaml` + `requirements-web.txt`)، ومسار القاعدة قابل للضبط عبر `BOOKING_DATA_DIR`.
المستودع: `alharbib902-del/booking-system` (خاص). الخطوات:
1. ادخل **render.com** → **New** → **Blueprint** → اربط GitHub → اختر مستودع `booking-system`.
2. Render يقرأ `render.yaml` تلقائياً. اضبط متغيّرات البيئة عند الطلب:
   - `BOOKING_OWNER_PASSWORD` = كلمة مرور لوحة المالك.
   - `WASENDER_API_KEY` = (اختياري) لإشعارات واتساب.
3. بعد النشر: `https://<اسم-الخدمة>.onrender.com/owner` (و `/b/<slug>` و `/admin/<slug>`).

**الاستمرارية:** الخطة المجانية قرصها مؤقت (للتجربة فقط — تُفقد البيانات عند إعادة التشغيل). للإنتاج: غيّر `plan` إلى `starter` وفعّل القرص الدائم في `render.yaml` واضبط `BOOKING_DATA_DIR=/var/data`.
> إدخال المنشآت للقاعدة السحابية يحتاج رفعها عبر لوحة المالك (ميزة قادمة) أو Render Shell — لأن `import` المحلي يكتب لقاعدة جهازك فقط.

### 👑 لوحة المالك (متابعة العملاء والاشتراكات)
بعد `python booking_system.py run`، افتح **http://localhost:5001/owner**
- كلمة المرور في `booking_data/.owner_pw` أو متغيّر `BOOKING_OWNER_PASSWORD`.
- تعرض: عدد العملاء، الاشتراكات النشطة، **الإيراد الشهري (MRR)**، إجمالي الحجوزات.
- تحرير اشتراك كل منشأة: الباقة، الحالة (تجريبي/نشط/موقوف/ملغى)، الرسوم الشهرية، تاريخ التجديد.

### 📲 أتمتة واتساب (WaSenderAPI)
أضِف مفتاحك بإحدى طريقتين (لا تكتبه في الكود):
- **الأسهل — ملف:** أنشئ ملف `wasender.key` في مجلد المشروع والصق فيه المفتاح فقط.
- **أو متغيّر بيئة:**
  ```powershell
  $env:WASENDER_API_KEY = "مفتاحك_من_wasenderapi.com"   # للجلسة الحالية
  setx WASENDER_API_KEY "مفتاحك_من_wasenderapi.com"      # دائم (يحتاج طرفية جديدة)
  ```
الأداة تقرأ المفتاح تلقائياً (الوسيط ← متغيّر البيئة ← `wasender.key`).
- **اختبر المفتاح** برسالة لرقمك: `python wasender.py --to 05xxxxxxxx`
- **حملة تسويقية** (تجريبي ثم فعلي): `python send_campaign.py` ← ثم `python send_campaign.py --send --rate 6 --limit 20`
- **إشعارات الحجز**: نظام الحجز يرسل تلقائياً إشعار واتساب للمنشأة عند كل حجز جديد (إن كان المفتاح مضبوطاً).
- ضمانات: وضع تجريبي افتراضي، تحديد معدّل، ومنع تكرار الإرسال.

**أسرع طريقة — أمر واحد:**
```bash
python pipeline.py --keyword "صالونات تجميل" --city "الرياض" --max 10 --import-booking
# ثم افتح:  output/outreach/index.html   (لوحة الحملة: نسخ رسالة / إرسال واتساب لكل منشأة)
```
ملفات تشغيل سريعة بنقرة: `start_pipeline.bat` · `start_booking.bat`

> 💡 نصيحة: استهدف فئات قليلة الرقمنة (مغاسل، ورش، صالونات صغيرة) → نسبة «بلا موقع» أعلى = فرص أكثر.
> ⚠️ تواصل برسالة فردية مهنية عبر القنوات العلنية (لا إرسال جماعي) احتراماً لنظام حماية البيانات.

---

## 📊 الحقول المُستخرجة لكل موقع

| الحقل | الوصف |
|---|---|
| `name` | اسم الموقع |
| `address` | العنوان |
| `category` | التصنيف |
| `rating` | التقييم (نجوم) |
| `reviews_count` | عدد المراجعات |
| `phone` | رقم الهاتف |
| `website` | الموقع الإلكتروني |
| `plus_code` | رمز الموقع |
| `price_range` | نطاق السعر |
| `opening_hours` | ساعات العمل |
| `is_open_now` | مفتوح الآن؟ |
| `latitude` / `longitude` | الإحداثيات |
| `place_url` | رابط الخريطة |
| `image_url` | صورة الموقع |
| `description` | الوصف |
| `timestamp` | وقت السحب |

---

## 📁 ملفات المشروع

```
google map/
├── scraper.py          ← منطق السحب (الفئة الرئيسية)
├── run.py              ← واجهة سطر الأوامر (3 أوضاع)
├── app.py              ← خادم الويب (Flask)
├── start_web.bat       ← تشغيل واجهة الويب
├── templates/index.html← صفحة الواجهة
├── static/style.css    ← تنسيق الواجهة
├── requirements.txt    ← المتطلبات
├── install.bat         ← تثبيت سريع
├── run.bat             ← تشغيل تفاعلي (CLI)
├── input_list.txt      ← قائمة الإدخال (عدّلها)
├── output/             ← النتائج (تُنشأ تلقائياً)
└── README.md
```

---

## 🛠️ ملاحظات تقنية

- Google Maps يغيّر بنية صفحته باستمرار. إذا توقفت حقول معينة عن السحب، قد تحتاج لتحديث selectors في `scraper.py`.
- للتقليل من الحظر: تجنّب آلاف الطلبات المتتالية، استخدم `--no-headless` لتتبع العمل، أو استخدم `--proxy`.
- الأداة تعمل بالعربية افتراضياً (`--lang ar`)؛ غيّرها لنتائج بلغة أخرى.

---

## ❓ استكشاف الأخطاء

- **`playwright not installed`**: شغّل `install.bat` أو `playwright install chromium`.
- **حقول فارغة**: جرّب `--lang en` (بعض الحقول تظهر بشكل مختلف حسب اللغة)، أو شغّل `--no-headless` لترى ما يحدث.
- **حظر مؤقت**: انتظر بضع دقائق، أو استخدم بروكسي، أو قلّل `--max`.
