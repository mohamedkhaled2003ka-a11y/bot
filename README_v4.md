# Telegram MCQ Bot — v4

ترقية كاملة من v3.1 (الملف الواحد + ملفات JSON) لمعمارية production فيها قاعدة بيانات، تصدير PDF/Anki، نظام إحالة، Webhooks، وإحصائيات.

---

## ما الجديد في v4؟

| الميزة | v3.1 | v4 |
|---|---|---|
| التخزين | ملفات JSON + `threading.Lock` | SQLite (أو Postgres) + SQLAlchemy 2.0 async |
| تصدير الأسئلة | Telegram polls فقط | + PDF + Anki `.apkg` |
| الإحالة | لا يوجد | كل يوزر له لينك، مكافآت لطرفين |
| التشغيل | Polling فقط | Webhooks أو Polling (auto-detect) |
| الإحصائيات | عدد المستخدمين فقط | `/stats`: أسئلة اليوم، Top 5، استهلاك كل مفتاح |

---

## هيكل المشروع

```
.
├── bot.py              ← الـ entry point، كل الـ handlers
├── db.py               ← SQLAlchemy models + async helpers
├── export.py           ← PDF (مع reshaping عربي) + Anki .apkg
├── migrate.py          ← سكريبت ينقل JSON → SQLite مرة واحدة
├── requirements.txt
├── fonts/
│   └── Amiri-Regular.ttf   ← (نزّله يدوياً، شوف تحت)
└── bot.db              ← يتعمل تلقائياً أول مرة تشغل
```

---

## التركيب

```bash
# 1. ثبّت المكتبات
pip install -r requirements.txt

# 2. نزّل خط Amiri (لازم للـ PDF بالعربي)
mkdir -p fonts
curl -L -o fonts/Amiri-Regular.ttf \
  https://github.com/aliftype/amiri/raw/master/fonts/Amiri-Regular.ttf

# 3. (اختياري) لو عندك JSON من v3.1، انقلها للـ DB:
python migrate.py --data-dir .

# 4. صدّر متغيرات البيئة (شوف الجدول تحت) ثم:
python bot.py
```

> الترحيل **idempotent**: لو شغّلته أكتر من مرة مفيش مشكلة، هيتجاهل الـIDs اللي موجودة. كمان `bot.py` بيستدعي الترحيل تلقائياً أول ما يقوم لو الـDB فاضي ولقى ملفات JSON جنبه.

---

## متغيرات البيئة

### مطلوبة

| المتغير | الوصف |
|---|---|
| `TELEGRAM_BOT_TOKEN` | توكن البوت من @BotFather |
| `GEMINI_API_KEY` *أو* `GEMINI_API_KEYS` | مفتاح Gemini واحد، أو list مفصولة بفاصلة (للـ pool) — يدعم لحد 20 مفتاح |

### اختيارية

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | الموديل المستخدم |
| `BOT_USERNAME` | _غير محدد_ | يوزرنيم البوت بدون `@` — لازم لتوليد روابط الإحالة |
| `REFERRAL_REWARD` | `3` | عدد المحاضرات الإضافية للطرفين عند نجاح إحالة |
| `DATABASE_URL` | `sqlite+aiosqlite:///bot.db` | استبدلها بـ `postgresql+asyncpg://...` لو هتستخدم Postgres |
| `BOT_DB_PATH` | `bot.db` | مسار ملف SQLite (يُتجاهل لو `DATABASE_URL` متغير) |
| `ARABIC_FONT_PATH` | `fonts/Amiri-Regular.ttf` | مسار الخط العربي للـPDF |
| `BOT_DATA_DIR` | `.` | مكان قراءة ملفات JSON القديمة عند الترحيل |

### Webhook (اختيارية — لو فاضية يفضل Polling)

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `WEBHOOK_URL` | _فاضي_ | الـURL العمومي للبوت (HTTPS). فاضي = polling |
| `WEBHOOK_LISTEN` | `0.0.0.0` | عنوان الاستماع الداخلي |
| `WEBHOOK_PORT` | `8443` | الـport الداخلي |
| `WEBHOOK_PATH` | `telegram` | الـpath بعد الـURL |
| `WEBHOOK_SECRET` | _فاضي_ | secret token (موصى به جداً) |

---

## نشر بـ Webhook (production)

**Telegram يفرض HTTPS وشهادة صحيحة.** الـapp نفسه بيرفع HTTP على البورت الداخلي، فالـ HTTPS termination بيعمله nginx (أو Caddy/Cloudflare).

مثال nginx مبسط:

```nginx
server {
    listen 443 ssl http2;
    server_name bot.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/bot.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.yourdomain.com/privkey.pem;

    location /telegram {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

ثم:

```bash
export WEBHOOK_URL="https://bot.yourdomain.com"
export WEBHOOK_LISTEN="127.0.0.1"
export WEBHOOK_PORT="8443"
export WEBHOOK_PATH="telegram"
export WEBHOOK_SECRET="$(openssl rand -hex 32)"
python bot.py
```

> لو شغّلت محلياً للتجربة: سيب `WEBHOOK_URL` فاضي، البوت هيقع تلقائياً على polling.

---

## الميزات الجديدة في الـ UI

### 1. تصدير الكويز
بعد ما البوت يبعت الـ polls، بيظهر رسالة فيها زرارين:
- 📄 **تحميل PDF** — ملف فيه كل الأسئلة + صفحة إجابات في الآخر
- 🃏 **تحميل Anki** — ملف `.apkg` يفتح مباشرة في Anki كـ flashcards

> الكاش بيخزّن آخر مجموعة أسئلة في `user_data`، يعني لازم تحمّل قبل ما تعمل كويز جديد.

### 2. نظام الإحالة
- زر جديد في كيبورد اليوزر: 🎁 **ادعو أصحابك**
- البوت بيبعت لينك شخصي: `t.me/<BOT_USERNAME>?start=ref_<USER_ID>`
- لو حد فتح اللينك ده أول مرة وضغط /start:
  - بيتعمله allow تلقائي (مفيش انتظار للأدمن)
  - بياخد +3 محاضرات هدية
  - اللي دعاه بياخد +3 كمان
- المكافآت تتحط في **bonus pool** منفصل عن الحد اليومي — والـ`consume` بيشرب من اليومي الأول، ولما يخلص يشرب من البونص.

### 3. الإحصائيات (للأدمن)
- زر جديد في لوحة الأدمن: 📈 **إحصائيات** (وكمان أمر `/stats`)
- بيوريك:
  - أسئلة اتولّدت اليوم vs آخر 7 أيام vs آخر 30 يوم
  - Top 5 يوزرز (بترتيب عدد المحاضرات المُولّدة)
  - استهلاك كل مفتاح Gemini (نجاح/فشل)

---

## النسخ الاحتياطي

ملف واحد بس: `bot.db`. أنصح بـ cron يومي:

```bash
# /etc/cron.daily/backup-mcq-bot
sqlite3 /path/to/bot.db ".backup '/backups/bot-$(date +\%F).db'"
```

لو على Postgres: `pg_dump` المعتاد.

---

## أوامر مفيدة

| الأمر | للأدمن | للوصف |
|---|---|---|
| `/start` | الكل | البداية + معالجة لينكات الإحالة |
| `/myid` | الكل | يظهر الـID بتاعك |
| `/cancel` | الكل | يلغي أي عملية معلقة |
| `/admin` | أدمن | لوحة التحكم |
| `/stats` | أدمن | الإحصائيات المتقدمة |

---

## استكشاف الأخطاء

**PDF بيطلع بدون نقط/حروف منفصلة؟**
يعني خط Amiri مش متحمّل. تأكد إن `fonts/Amiri-Regular.ttf` موجود، أو ظبط `ARABIC_FONT_PATH` على مسار الخط عندك.

**Webhook بيرجّع 401؟**
في الغالب `WEBHOOK_SECRET` مختلف بين الـenv والـ Telegram. شغّل البوت تاني عشان يعيد set الـwebhook.

**`/start ref_xxx` مش بيدّي مكافأة؟**
لازم اليوزر يكون **جديد** (مش في الـDB قبل كده). الإحالة مش بتتسجل لمستخدم متفعّل من قبل.

**الترحيل من v3.1؟**
شغّل `python migrate.py --data-dir <path-to-json-files>` مرة واحدة قبل أول تشغيل لـ v4. مفيش حاجة بتتمسح من الـJSON القديم، فأنت آمن.

---

## أمان

- `WEBHOOK_SECRET` لازم في production — بدونه أي حد يقدر يبعت updates مزيفة على الـ endpoint.
- `bot.db` فيه IDs وبيانات يوزرز — تأكد إن الـفايل بصلاحيات `600` ومش في git.
- مفاتيح Gemini كلها في الـenv، مش في الكود.
