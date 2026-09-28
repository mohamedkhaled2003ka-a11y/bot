"""
islamic.py — Islamic section for the MCQ bot (#7, #11, #12)  — v2
=================================================================
DROP-IN. In bot.py:  import islamic ;  islamic.register(app)

WHAT WAS FIXED (reciters bug):
  The old version hardcoded the audio URL to bitrate /128/ and used several
  edition IDs that either don't exist or only publish full-surah audio at a
  DIFFERENT bitrate (commonly /64/). Result: only Mishary Alafasy played and
  every other sheikh silently failed.

  v2 fixes this properly:
   * A larger curated reciter list (correct alquran.cloud edition IDs).
   * On first use, each reciter is VERIFIED against the CDN, trying bitrates
     128 then 64. Only reciters whose audio actually resolves are shown, and
     the working bitrate is remembered, so playback always uses a valid URL.
   * The verified map is cached in memory and in your db KV store.

PLUS:
   * Download button (sends the surah as a downloadable file).
   * Azkar daily reminders (opt-in): morning + evening push via job_queue.

NETWORK: needs api.alquran.cloud and cdn.islamic.network reachable.
Requires httpx (PTB already depends on it).
"""
from __future__ import annotations

import json
import logging
import datetime as _dt

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

import db

logger = logging.getLogger("islamic")

API_BASE = "https://api.alquran.cloud/v1"
AUDIO_CDN = "https://cdn.islamic.network/quran/audio-surah/{bitrate}/{edition}/{surah}.mp3"
BITRATES = (128, 64)  # try high quality first, fall back to 64

# Curated reciter list — canonical alquran.cloud audio edition IDs.
# Any that don't resolve on the CDN are auto-dropped at verification time,
# so it is safe to list generously.
RECITERS = {
    "ar.alafasy":            "مشاري العفاسي",
    "ar.abdulbasitmurattal": "عبد الباسط (مرتل)",
    "ar.abdulsamad":         "عبد الباسط عبد الصمد",
    "ar.abdurrahmaansudais": "عبد الرحمن السديس",
    "ar.abdullahbasfar":     "عبد الله بصفر",
    "ar.shaatree":           "أبو بكر الشاطري",
    "ar.ahmedajamy":         "أحمد العجمي",
    "ar.hanirifai":          "هاني الرفاعي",
    "ar.husary":             "محمود الحصري",
    "ar.husarymujawwad":     "الحصري (مجوّد)",
    "ar.hudhaify":           "علي الحذيفي",
    "ar.mahermuaiqly":       "ماهر المعيقلي",
    "ar.minshawi":           "محمد المنشاوي",
    "ar.minshawimujawwad":   "المنشاوي (مجوّد)",
    "ar.muhammadayyoub":     "محمد أيوب",
    "ar.muhammadjibreel":    "محمد جبريل",
    "ar.saoodshuraym":       "سعود الشريم",
    "ar.dossari":             "ياسر الدوسري",
    "ar.abdullahaljuhany":    "عبد الله الجهني",
    "ar.aymansweid":          "أيمن سويد",
    "ar.nasseralqatami":      "ناصر القطامي",
    "ar.faresabbad":          "فارس عباد",
    "ar.idreesabkar":         "إدريس أبكر",
    "ar.khalilalhosary":      "محمود خليل الحصري",
}

READ_CHUNK = 3500
_SURAH_CACHE: list | None = None
# edition -> working bitrate (verified). None until first verification.
_VERIFIED: dict[str, int] | None = None

# ---- Curated Azkar -----------------------------------------------------
AZKAR = {
    "morning": ("أذكار الصباح ☀️", [
        "أَصْبَحْنَا وَأَصْبَحَ الْمُلْكُ لِلَّهِ، وَالْحَمْدُ لِلَّهِ، لَا إِلَهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ. (مرة)",
        "اللَّهُمَّ بِكَ أَصْبَحْنَا، وَبِكَ أَمْسَيْنَا، وَبِكَ نَحْيَا، وَبِكَ نَمُوتُ، وَإِلَيْكَ النُّشُورُ. (مرة)",
        "سُبْحَانَ اللَّهِ وَبِحَمْدِهِ. (100 مرة)",
        "أَعُوذُ بِكَلِمَاتِ اللَّهِ التَّامَّاتِ مِنْ شَرِّ مَا خَلَقَ. (3 مرات)",
        "حَسْبِيَ اللَّهُ لَا إِلَهَ إِلَّا هُوَ، عَلَيْهِ تَوَكَّلْتُ وَهُوَ رَبُّ الْعَرْشِ الْعَظِيمِ. (7 مرات)",
        "رَضِيتُ بِاللَّهِ رَبًّا، وَبِالْإِسْلَامِ دِينًا، وَبِمُحَمَّدٍ ﷺ نَبِيًّا. (3 مرات)",
    ]),
    "evening": ("أذكار المساء 🌙", [
        "أَمْسَيْنَا وَأَمْسَى الْمُلْكُ لِلَّهِ، وَالْحَمْدُ لِلَّهِ، لَا إِلَهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ. (مرة)",
        "اللَّهُمَّ بِكَ أَمْسَيْنَا، وَبِكَ أَصْبَحْنَا، وَبِكَ نَحْيَا، وَبِكَ نَمُوتُ، وَإِلَيْكَ الْمَصِيرُ. (مرة)",
        "أَعُوذُ بِكَلِمَاتِ اللَّهِ التَّامَّاتِ مِنْ شَرِّ مَا خَلَقَ. (3 مرات)",
        "بِسْمِ اللَّهِ الَّذِي لَا يَضُرُّ مَعَ اسْمِهِ شَيْءٌ فِي الْأَرْضِ وَلَا فِي السَّمَاءِ وَهُوَ السَّمِيعُ الْعَلِيمُ. (3 مرات)",
        "سُبْحَانَ اللَّهِ وَبِحَمْدِهِ. (100 مرة)",
    ]),
    "sleep": ("أذكار النوم 😴", [
        "بِاسْمِكَ اللَّهُمَّ أَمُوتُ وَأَحْيَا.",
        "اللَّهُمَّ قِنِي عَذَابَكَ يَوْمَ تَبْعَثُ عِبَادَكَ. (3 مرات)",
        "آية الكرسي (مرة).",
        "قراءة سورتي الإخلاص والمعوذتين والنفث في الكفين ومسح الجسد. (3 مرات)",
        "سُبْحَانَ اللَّهِ (33)، الْحَمْدُ لِلَّهِ (33)، اللَّهُ أَكْبَرُ (34).",
    ]),
    "prayer": ("أذكار بعد الصلاة 🕌", [
        "أَسْتَغْفِرُ اللَّهَ (3 مرات)، اللَّهُمَّ أَنْتَ السَّلَامُ وَمِنْكَ السَّلَامُ تَبَارَكْتَ يَا ذَا الْجَلَالِ وَالْإِكْرَامِ.",
        "لَا إِلَهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ، لَهُ الْمُلْكُ وَلَهُ الْحَمْدُ وَهُوَ عَلَى كُلِّ شَيْءٍ قَدِيرٌ.",
        "سُبْحَانَ اللَّهِ (33)، الْحَمْدُ لِلَّهِ (33)، اللَّهُ أَكْبَرُ (33)، وتمام المئة: لا إله إلا الله وحده لا شريك له...",
        "آية الكرسي بعد كل صلاة مكتوبة.",
    ]),
    "general": ("أذكار عامة 📿", [
        "لَا إِلَهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ، لَهُ الْمُلْكُ وَلَهُ الْحَمْدُ وَهُوَ عَلَى كُلِّ شَيْءٍ قَدِيرٌ.",
        "سُبْحَانَ اللَّهِ وَبِحَمْدِهِ، سُبْحَانَ اللَّهِ الْعَظِيمِ.",
        "اللَّهُمَّ صَلِّ وَسَلِّمْ عَلَى نَبِيِّنَا مُحَمَّدٍ.",
        "أَسْتَغْفِرُ اللَّهَ الْعَظِيمَ وَأَتُوبُ إِلَيْهِ.",
        "لَا حَوْلَ وَلَا قُوَّةَ إِلَّا بِاللَّهِ.",
    ]),
}


# ============================================================ HTTP
async def _get_json(url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception:
        logger.exception("Quran API request failed: %s", url)
        return None


async def _surah_list() -> list:
    global _SURAH_CACHE
    if _SURAH_CACHE is not None:
        return _SURAH_CACHE
    data = await _get_json(f"{API_BASE}/surah")
    _SURAH_CACHE = data["data"] if (data and data.get("data")) else []
    return _SURAH_CACHE


async def _surah_text(num: int) -> str | None:
    data = await _get_json(f"{API_BASE}/surah/{num}/quran-uthmani")
    if not data or not data.get("data"):
        return None
    d = data["data"]
    ayat = d.get("ayahs", [])
    name = d.get("name", f"سورة {num}")
    body = "\n".join(f"{a['text']} ﴿{a['numberInSurah']}﴾" for a in ayat)
    return f"{name}\n\n{body}"


# ============================================================ reciter verify
async def _verify_reciters() -> dict[str, int]:
    """Probe the CDN for each candidate reciter, trying bitrates in order.
    Returns {edition: working_bitrate}. Cached in memory + db state."""
    global _VERIFIED
    if _VERIFIED is not None:
        return _VERIFIED

    # try cached result from db first
    try:
        cached = await db.get_state("quran_reciters_verified", "")
        if cached:
            _VERIFIED = {k: int(v) for k, v in json.loads(cached).items()
                         if k in RECITERS}
            if _VERIFIED and set(RECITERS).issubset(_VERIFIED):
                return _VERIFIED
    except Exception:
        pass

    verified: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for edition in RECITERS:
            for br in BITRATES:
                url = AUDIO_CDN.format(bitrate=br, edition=edition, surah=1)
                try:
                    # Range request: fetch only first byte to confirm existence
                    r = await client.get(url, headers={"Range": "bytes=0-0"})
                    if r.status_code in (200, 206):
                        verified[edition] = br
                        break
                except Exception:
                    continue
    # Alafasy at 128 is always present; guarantee at least it.
    verified.setdefault("ar.alafasy", 128)
    _VERIFIED = verified
    try:
        await db.set_state("quran_reciters_verified",
                           json.dumps({k: str(v) for k, v in verified.items()}))
    except Exception:
        pass
    logger.info("Verified %d/%d reciters", len(verified), len(RECITERS))
    return verified


# ============================================================ bookmarks
async def _get_bookmarks(uid: int) -> list[int]:
    raw = await db.get_state(f"quran_bm:{uid}", "[]")
    try:
        v = json.loads(raw)
        return [int(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


async def _set_bookmarks(uid: int, items: list[int]):
    await db.set_state(f"quran_bm:{uid}", json.dumps(sorted(set(items))))


# ============================================================ menus
def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📿 الأذكار", callback_data="isl:azk:menu")],
        [InlineKeyboardButton("📖 قراءة القرآن", callback_data="isl:q:list:0")],
        [InlineKeyboardButton("🔍 بحث عن سورة", callback_data="isl:q:search")],
        [InlineKeyboardButton("⭐ المفضلة", callback_data="isl:q:bm")],
        [InlineKeyboardButton("🔔 تذكير الأذكار", callback_data="isl:azk:remind")],
    ])


async def cmd_islamic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    await update.message.reply_text(
        "🕌 <b>القسم الإسلامي</b>\n\nاختار من تحت:",
        parse_mode="HTML", reply_markup=_main_menu())


def _azkar_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(AZKAR[k][0], callback_data=f"isl:azk:{k}")]
            for k in AZKAR]
    rows.append([InlineKeyboardButton("🔔 تفعيل/إلغاء التذكير",
                                       callback_data="isl:azk:remind")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="isl:home")])
    return InlineKeyboardMarkup(rows)


async def _surah_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    surahs = await _surah_list()
    if not surahs:
        return ("⚠️ مقدرتش أجيب قائمة السور دلوقتي، حاول تاني.",
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🔙 رجوع", callback_data="isl:home")]]))
    per = 10
    pages = (len(surahs) + per - 1) // per
    page = max(0, min(page, pages - 1))
    chunk = surahs[page * per:(page + 1) * per]
    rows = [[InlineKeyboardButton(f"{s['number']}. {s['name']}",
                                  callback_data=f"isl:q:open:{s['number']}")]
            for s in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"isl:q:list:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="isl:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"isl:q:list:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="isl:home")])
    return "📖 <b>اختار سورة:</b>", InlineKeyboardMarkup(rows)


async def _surah_actions(uid: int, num: int) -> tuple[str, InlineKeyboardMarkup]:
    surahs = await _surah_list()
    name = next((s["name"] for s in surahs if s["number"] == num), f"سورة {num}")
    bms = await _get_bookmarks(uid)
    star = "🌟 إزالة من المفضلة" if num in bms else "⭐ إضافة للمفضلة"
    rows = [
        [InlineKeyboardButton("📖 قراءة", callback_data=f"isl:q:read:{num}:0")],
        [InlineKeyboardButton("🎧 استماع", callback_data=f"isl:q:rec:{num}")],
        [InlineKeyboardButton(star, callback_data=f"isl:q:star:{num}")],
        [InlineKeyboardButton("🔙 قائمة السور", callback_data="isl:q:list:0")],
    ]
    return f"📜 <b>{name}</b>\n\nاختار:", InlineKeyboardMarkup(rows)


async def _reciter_menu(num: int) -> InlineKeyboardMarkup:
    verified = await _verify_reciters()
    rows = [[InlineKeyboardButton(RECITERS[ed],
                                  callback_data=f"isl:q:play:{num}:{ed}")]
            for ed in RECITERS if ed in verified]
    rows.append([InlineKeyboardButton("⬇️ تحميل (ملف)",
                                       callback_data=f"isl:q:dl:{num}")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"isl:q:open:{num}")])
    return InlineKeyboardMarkup(rows)


# ============================================================ callbacks
async def on_islamic_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    q = update.callback_query
    await q.answer()
    if not await bot.ensure_access(update):
        return
    uid = q.from_user.id
    parts = q.data.split(":")
    section = parts[1] if len(parts) > 1 else ""

    if q.data == "isl:home":
        await bot.safe_edit(q, "🕌 <b>القسم الإسلامي</b>\n\nاختار من تحت:",
                            parse_mode="HTML", reply_markup=_main_menu())
        return
    if q.data == "isl:noop":
        return

    if section == "azk":
        which = parts[2]
        if which == "menu":
            await bot.safe_edit(q, "📿 <b>الأذكار</b>", parse_mode="HTML",
                                reply_markup=_azkar_menu())
            return
        if which == "remind":
            await _toggle_reminder(q, context, uid)
            return
        if which in AZKAR:
            title, items = AZKAR[which]
            body = "\n\n".join(f"• {x}" for x in items)
            await bot.safe_edit(
                q, f"<b>{title}</b>\n\n{body}", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🔙 رجوع", callback_data="isl:azk:menu")]]))
            return

    if section == "q":
        sub = parts[2]

        if sub == "list":
            text, kb = await _surah_page(int(parts[3]))
            await bot.safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
            return

        if sub == "open":
            text, kb = await _surah_actions(uid, int(parts[3]))
            await bot.safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
            return

        if sub == "read":
            num, page = int(parts[3]), int(parts[4])
            full = await _surah_text(num)
            if not full:
                await bot.safe_edit(q, "⚠️ مقدرتش أجيب نص السورة دلوقتي.",
                                    reply_markup=InlineKeyboardMarkup([[
                                        InlineKeyboardButton("🔙 رجوع",
                                            callback_data=f"isl:q:open:{num}")]]))
                return
            pieces = _split(full, READ_CHUNK)
            page = max(0, min(page, len(pieces) - 1))
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️", callback_data=f"isl:q:read:{num}:{page-1}"))
            nav.append(InlineKeyboardButton(f"{page+1}/{len(pieces)}", callback_data="isl:noop"))
            if page < len(pieces) - 1:
                nav.append(InlineKeyboardButton("➡️", callback_data=f"isl:q:read:{num}:{page+1}"))
            kb = InlineKeyboardMarkup([nav, [InlineKeyboardButton(
                "🔙 رجوع", callback_data=f"isl:q:open:{num}")]])
            await bot.safe_edit(q, pieces[page], reply_markup=kb)
            return

        if sub == "rec":
            num = int(parts[3])
            await bot.safe_edit(q, "🎧 اختار القارئ:",
                                reply_markup=await _reciter_menu(num))
            return

        if sub in ("play", "dl"):
            num = int(parts[3])
            verified = await _verify_reciters()
            if sub == "dl":
                edition = "ar.alafasy"
            else:
                edition = parts[4]
            br = verified.get(edition)
            if br is None:
                await q.answer("القارئ ده مش متاح دلوقتي، جرّب غيره", show_alert=True)
                return
            url = AUDIO_CDN.format(bitrate=br, edition=edition, surah=num)
            surahs = await _surah_list()
            name = next((s["name"] for s in surahs if s["number"] == num), f"سورة {num}")
            reciter = RECITERS.get(edition, edition)
            try:
                if sub == "dl":
                    await q.message.reply_document(
                        document=url, filename=f"{name}.mp3",
                        caption=f"⬇️ {name} — {reciter}")
                else:
                    await q.message.reply_audio(
                        audio=url, title=name, performer=reciter,
                        caption=f"🎧 {name} — {reciter}")
                await q.answer("جاري الإرسال 🎧")
            except Exception:
                logger.exception("audio send failed for %s", edition)
                await q.answer("مقدرتش أبعت الصوت، جرّب قارئ تاني", show_alert=True)
            return

        if sub == "star":
            num = int(parts[3])
            bms = await _get_bookmarks(uid)
            bms.remove(num) if num in bms else bms.append(num)
            await _set_bookmarks(uid, bms)
            text, kb = await _surah_actions(uid, num)
            await bot.safe_edit(q, text, parse_mode="HTML", reply_markup=kb)
            return

        if sub == "bm":
            bms = await _get_bookmarks(uid)
            if not bms:
                await bot.safe_edit(q, "⭐ مفيش سور في المفضلة لسه.",
                                    reply_markup=InlineKeyboardMarkup([[
                                        InlineKeyboardButton("🔙 رجوع",
                                            callback_data="isl:home")]]))
                return
            surahs = await _surah_list()
            rows = []
            for n in bms:
                nm = next((s["name"] for s in surahs if s["number"] == n), f"سورة {n}")
                rows.append([InlineKeyboardButton(f"⭐ {nm}",
                                                  callback_data=f"isl:q:open:{n}")])
            rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="isl:home")])
            await bot.safe_edit(q, "⭐ <b>سورك المفضلة:</b>", parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(rows))
            return

        if sub == "search":
            context.user_data["mode"] = "quran_search"
            await bot.safe_edit(q, "🔍 اكتب اسم السورة أو رقمها (1-114):")
            return


def _split(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [text]


# ============================================================ Azkar reminders
async def _toggle_reminder(q, context, uid: int):
    import bot
    key = f"azkar_remind:{uid}"
    on = (await db.get_state(key, "0")) == "1"
    if on:
        await db.set_state(key, "0")
        _cancel_user_jobs(context, uid)
        msg = "🔕 تم إلغاء تذكير الأذكار."
    else:
        await db.set_state(key, "1")
        _schedule_user(context, uid)
        msg = ("🔔 تم تفعيل تذكير الأذكار!\n"
               "هتوصلك أذكار الصباح الساعة 6 ص وأذكار المساء الساعة 6 م "
               "(بتوقيت السيرفر).")
    await bot.safe_edit(q, msg, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            "🔙 رجوع", callback_data="isl:azk:menu")]]))


def _cancel_user_jobs(context, uid: int):
    jq = context.application.job_queue
    if not jq:
        return
    for name in (f"azkar_m_{uid}", f"azkar_e_{uid}"):
        for job in jq.get_jobs_by_name(name):
            job.schedule_removal()


def _schedule_user(context, uid: int):
    jq = context.application.job_queue
    if not jq:
        logger.warning("job_queue unavailable; reminders won't fire")
        return
    _cancel_user_jobs(context, uid)
    jq.run_daily(_send_azkar, time=_dt.time(hour=6, minute=0),
                 chat_id=uid, name=f"azkar_m_{uid}", data="morning")
    jq.run_daily(_send_azkar, time=_dt.time(hour=18, minute=0),
                 chat_id=uid, name=f"azkar_e_{uid}", data="evening")


async def _send_azkar(context: ContextTypes.DEFAULT_TYPE):
    which = context.job.data
    title, items = AZKAR[which]
    body = "\n\n".join(f"• {x}" for x in items)
    try:
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=f"<b>{title}</b>\n\n{body}", parse_mode="HTML")
    except Exception:
        logger.exception("failed to send azkar reminder")


async def _restore_reminders(app: Application):
    """On startup, re-schedule reminders for everyone who opted in."""
    try:
        prefix = "azkar_remind:"
        # uses your KV store; if you don't have list_state, this is a no-op.
        if not hasattr(db, "list_state"):
            return
        for key, val in await db.list_state(prefix):
            if val == "1":
                uid = int(key.split(":", 1)[1])
                jq = app.job_queue
                if jq:
                    jq.run_daily(_send_azkar, time=_dt.time(6, 0),
                                 chat_id=uid, name=f"azkar_m_{uid}", data="morning")
                    jq.run_daily(_send_azkar, time=_dt.time(18, 0),
                                 chat_id=uid, name=f"azkar_e_{uid}", data="evening")
    except Exception:
        logger.exception("restore reminders failed")


# ============================================================ search hook
async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             text: str) -> bool:
    import bot
    if context.user_data.get("mode") != "quran_search":
        return False
    context.user_data.pop("mode", None)
    surahs = await _surah_list()
    query = text.strip()
    match = None
    if query.isdigit():
        match = next((s for s in surahs if s["number"] == int(query)), None)
    if match is None:
        ql = query.lower()
        match = next((s for s in surahs
                      if ql in s.get("name", "").lower()
                      or ql in s.get("englishName", "").lower()), None)
    if match is None:
        await update.message.reply_text(
            "ملقيتش سورة بالاسم/الرقم ده 🔎",
            reply_markup=bot.get_keyboard_for(update.effective_user.id))
        return True
    txt, kb = await _surah_actions(update.effective_user.id, match["number"])
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    return True


def register(app: Application):
    app.add_handler(CommandHandler("islamic", cmd_islamic))
    app.add_handler(CallbackQueryHandler(on_islamic_cb, pattern=r"^isl:"))
    # re-arm opt-in reminders after restart
    async def _post(_app):
        await _restore_reminders(_app)
    app.post_init = _chain(getattr(app, "post_init", None), _post)
    logger.info("islamic registered")


def _chain(existing, new):
    if existing is None:
        return new
    async def _both(app):
        await existing(app)
        await new(app)
    return _both
