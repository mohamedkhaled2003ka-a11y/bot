"""
addons.py — feature add-ons for the MCQ bot (v5)
================================================

This module is DROP-IN. It does not modify your existing handlers; it
registers its own handlers in a higher-priority group (-1) and uses
ApplicationHandlerStop so your original handlers still run for everything
it does not claim.

Integration (in bot.py):

    import addons
    ...
    # after app = Application.builder()...build()
    addons.register(app)

It also exposes `addons.generate_mcqs_smart(...)` which you should call from
`generate_mcqs_and_send` instead of the three separate generate_* calls — it
batches large requests so 100/200+ questions no longer truncate or fail.

Everything here imports `bot` lazily inside functions to avoid a circular
import, and only uses the public `db` functions your bot already calls.
"""

from __future__ import annotations

import io
import json
import asyncio
import logging
try:
    import resource
except ImportError:
    resource = None
import time
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ApplicationHandlerStop,
    filters,
)
from google.genai import types

import db

logger = logging.getLogger("addons")

# Per-request safe batch size. Each Gemini call is capped by MAX_OUTPUT_TOKENS
# (8192 in your config), which is roughly ~25-30 well-formed MCQs before the
# JSON gets truncated and parsing fails. We split big requests into batches
# and run a few in parallel.
QUESTIONS_PER_BATCH = 25
MAX_PARALLEL_BATCHES = 4

# Spot-diagnosis / quiz-from-file modes (stored in context.user_data["mode"])
MODE_SPOT = "spot_diag"
MODE_QFILE = "qfile"

# Background-task registry for /tasks and /health
_ACTIVE_TASKS: dict[int, dict] = {}
_TASK_SEQ = 0
_START_TIME = time.time()


# ============================================================
# Background task tracking (Performance & Stability #8 / Admin #9)
# ============================================================
def track(name: str, coro):
    """Wrap a coroutine in a tracked background task. Returns the task."""
    global _TASK_SEQ
    _TASK_SEQ += 1
    tid = _TASK_SEQ

    async def _runner():
        _ACTIVE_TASKS[tid] = {"name": name, "started": time.time()}
        try:
            return await coro
        except Exception:
            logger.exception("Tracked task '%s' crashed", name)
        finally:
            _ACTIVE_TASKS.pop(tid, None)

    return asyncio.create_task(_runner())


def _human_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not bot.is_admin(update.effective_user.id):
        return
    now = time.time()
    if not _ACTIVE_TASKS:
        await update.message.reply_text("✅ مفيش مهام شغالة في الخلفية دلوقتي.")
        return
    lines = ["⚙️ <b>المهام الشغالة في الخلفية:</b>", ""]
    for tid, info in list(_ACTIVE_TASKS.items()):
        lines.append(f"#{tid} — {bot.html_escape(info['name'])} "
                     f"(منذ {_human_age(now - info['started'])})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not bot.is_admin(update.effective_user.id):
        return
    now = time.time()
    # Max RSS in KB on Linux.
    rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss_kb / 1024
    active_keys = 0
    total_keys = 0
    if bot.KEY_POOL:
        total_keys = len(bot.KEY_POOL)
        active_keys = sum(1 for i in range(total_keys)
                          if bot.KEY_POOL.cooldown_until.get(i, 0) <= now)
    total_u = await db.count_allowed_users()
    text = (
        "🩺 <b>صحة النظام</b>\n\n"
        f"⏱ Uptime: {_human_age(now - _START_TIME)}\n"
        f"🧠 Memory (max RSS): {rss_mb:.0f} MB\n"
        f"⚙️ مهام شغالة: {len(_ACTIVE_TASKS)}\n"
        f"🔑 مفاتيح Gemini نشطة: {active_keys}/{total_keys}\n"
        f"👥 مستخدمين: {total_u}\n"
        f"🤖 الحالة: {'✅ شغال' if bot._BOT_ENABLED else '🛑 متوقف'}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ============================================================
# Iterate ALL users via the public paginated API
# ============================================================
async def _iter_all_users(page_size: int = 100):
    page = 0
    while True:
        users, total = await db.list_users_page(page, page_size)
        if not users:
            return
        for u in users:
            yield u
        page += 1
        if page * page_size >= total:
            return


def _user_to_dict(u) -> dict:
    return {
        "id": u.id,
        "username": u.username or "",
        "first_name": u.first_name or "",
        "last_name": getattr(u, "last_name", "") or "",
        "allowed": bool(u.allowed),
        "tier": u.tier,
        "daily_limit": u.daily_limit,
        "used_today": u.used_today,
        "bonus_lectures": u.bonus_lectures,
        "anonymous": bool(u.anonymous),
        "protect_content": bool(u.protect_content),
        "smart_chat_enabled": bool(u.smart_chat_enabled),
        "total_generated": u.total_generated,
        "referrals_made": u.referrals_made,
        "last_seen": u.last_seen or "",
    }


# ============================================================
# Backup / Restore  (#1)
# ============================================================
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    uid = update.effective_user.id
    if not bot.is_admin(uid):
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    rows = [_user_to_dict(u) async for u in _iter_all_users()]
    payload = {
        "schema": "mcqbot-users-backup",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "users": rows,
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"users_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    await update.message.reply_document(
        document=InputFile(io.BytesIO(data), filename=fname),
        caption=(f"💾 نسخة احتياطية: <b>{len(rows)}</b> مستخدم.\n"
                 f"احتفظ بالملف ده. للاستعادة ابعت /restore وارفقه."),
        parse_mode="HTML",
    )


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    uid = update.effective_user.id
    if not bot.is_admin(uid):
        return
    context.user_data["addon_action"] = "awaiting_restore"
    bot.touch_state(context)
    await update.message.reply_text(
        "♻️ <b>استعادة المستخدمين</b>\n\n"
        "ابعت ملف الـ backup (JSON) اللي حفظته قبل كده.\n"
        "هيتم استرجاع: الصلاحية + الباقة + الإعدادات + الرصيد الإضافي.\n\n"
        "<i>/cancel للإلغاء</i>",
        parse_mode="HTML",
    )


async def _do_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    context.user_data.pop("addon_action", None)
    doc = update.message.document
    status = await update.message.reply_text("جاري الاستعادة… ⏳")
    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
        payload = json.loads(bytes(raw).decode("utf-8"))
        users = payload.get("users", [])
        assert isinstance(users, list)
    except Exception as e:
        await status.edit_text(f"❌ ملف غير صالح: {bot.html_escape(str(e))}",
                               parse_mode="HTML")
        return

    restored = skipped = failed = 0
    for rec in users:
        try:
            rid = int(rec["id"])
        except (KeyError, ValueError, TypeError):
            failed += 1
            continue
        if not rec.get("allowed", True):
            skipped += 1
            continue
        try:
            await db.allow_user(rid)
            tier = rec.get("tier")
            if tier in bot.TIERS:
                await db.set_tier(rid, tier,
                                  rec.get("daily_limit", bot.TIERS[tier]["daily_limit"]))
            await db.set_setting(rid, "anonymous", bool(rec.get("anonymous", False)))
            await db.set_setting(rid, "protect_content",
                                 bool(rec.get("protect_content", False)))
            await db.set_setting(rid, "smart_chat_enabled",
                                 bool(rec.get("smart_chat_enabled", False)))
            bonus = int(rec.get("bonus_lectures", 0) or 0)
            if bonus > 0:
                await db.add_bonus(rid, bonus)
            restored += 1
        except Exception:
            logger.exception("restore failed for %s", rec.get("id"))
            failed += 1

    await status.edit_text(
        f"✅ <b>تمت الاستعادة</b>\n\n"
        f"♻️ اتسترجعوا: <b>{restored}</b>\n"
        f"⏭ اتخطّوا: <b>{skipped}</b>\n"
        f"⚠️ فشلوا: <b>{failed}</b>\n\n"
        f"<i>ملاحظة: الرصيد الإضافي بيتضاف (additive) — متعملش restore لنفس "
        f"الملف أكتر من مرة عشان ميتكررش.</i>",
        parse_mode="HTML",
    )


# ============================================================
# Large-set batched generation  (#3)
# ============================================================
async def generate_mcqs_smart(*, text=None, pdf_images=None,
                              image_data=None, image_mime=None,
                              n=10, difficulty="mixed", language="auto"):
    """Generate `n` MCQs, batching large requests so big sets don't truncate.

    Call this from generate_mcqs_and_send instead of the three separate
    generate_mcqs_from_* calls. Returns a merged list of MCQ dicts.
    """
    import bot
    pdf_images = pdf_images or []

    async def make_one(k: int):
        if pdf_images or (text and not image_data):
            return await bot.generate_mcqs_from_pdf(text, pdf_images, k,
                                                    difficulty, language)
        if image_data:
            return await bot.generate_mcqs_from_image(image_data, image_mime, k,
                                                      difficulty, language)
        return await bot.generate_mcqs_from_text(text, k, difficulty, language)

    if n <= QUESTIONS_PER_BATCH:
        return await make_one(n)

    # Split into batches.
    batches, remaining = [], n
    while remaining > 0:
        batches.append(min(QUESTIONS_PER_BATCH, remaining))
        remaining -= batches[-1]

    sem = asyncio.Semaphore(MAX_PARALLEL_BATCHES)

    async def run(k):
        async with sem:
            try:
                return await make_one(k)
            except Exception:
                logger.exception("MCQ batch (%d) failed", k)
                return []

    chunks = await asyncio.gather(*[run(k) for k in batches])
    merged: list = []
    seen_q: set = set()
    for c in chunks:
        for item in (c or []):
            key = str(item.get("q", "")).strip().lower()[:120]
            if key and key in seen_q:
                continue
            seen_q.add(key)
            merged.append(item)
    return merged


# ============================================================
# Smart-mode dashboard + bulk  (#4)
# ============================================================
async def cmd_smartdash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not bot.is_admin(update.effective_user.id):
        return
    text, kb = await _build_smartdash(0)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def _build_smartdash(page: int):
    import bot
    enabled = disabled = 0
    enabled_users = []
    async for u in _iter_all_users():
        if u.smart_chat_enabled:
            enabled += 1
            enabled_users.append(u)
        else:
            disabled += 1

    per = 8
    pages = max(1, (len(enabled_users) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    chunk = enabled_users[page * per:(page + 1) * per]

    lines = [
        "🤖 <b>لوحة الوضع الذكي</b>",
        "",
        f"✅ مفعّل: <b>{enabled}</b>",
        f"❌ متعطل: <b>{disabled}</b>",
        "",
    ]
    if chunk:
        lines.append(f"<b>المفعّلين (صفحة {page+1}/{pages}):</b>")
        for u in chunk:
            name = bot.html_escape(u.first_name or u.username or str(u.id))
            lines.append(f"• {name} (<code>{u.id}</code>)")
    else:
        lines.append("<i>مفيش حد مفعّل عنده الوضع الذكي.</i>")

    rows = []
    for u in chunk:
        name = (u.first_name or u.username or str(u.id))[:20]
        rows.append([InlineKeyboardButton(
            f"❌ تعطيل {name}", callback_data=f"addon:sm:tog:{u.id}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"addon:sm:page:{page-1}"))
    nav.append(InlineKeyboardButton("🔄", callback_data=f"addon:sm:page:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"addon:sm:page:{page+1}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("✅ تفعيل للكل", callback_data="addon:sm:bulk:on"),
        InlineKeyboardButton("❌ تعطيل للكل", callback_data="addon:sm:bulk:off"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def on_smartdash_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    q = update.callback_query
    await q.answer()
    if not bot.is_admin(q.from_user.id):
        await q.answer("مش أدمن ❌", show_alert=True)
        return
    parts = q.data.split(":")  # addon:sm:<sub>:...
    sub = parts[2]
    if sub == "page":
        page = int(parts[3])
    elif sub == "tog":
        uid = int(parts[3]); page = int(parts[4])
        u = await db.get_user(uid)
        if u:
            await db.set_setting(uid, "smart_chat_enabled", not u.smart_chat_enabled)
    elif sub == "bulk":
        target = parts[3] == "on"
        count = 0
        async for u in _iter_all_users():
            await db.set_setting(u.id, "smart_chat_enabled", target)
            count += 1
        await q.answer(f"تم تحديث {count} مستخدم", show_alert=True)
        page = 0
    else:
        page = 0
    text, kb = await _build_smartdash(page)
    await bot.safe_edit(q, text, parse_mode="HTML", reply_markup=kb)


# ============================================================
# Quiz from a question file (PDF / DOCX / TXT)  (#5)
# ============================================================
def _extract_text_any(file_bytes: bytes, fname: str, mime: str) -> str:
    import bot
    fname = (fname or "").lower()
    mime = (mime or "").lower()
    if fname.endswith(".pdf") or mime == "application/pdf":
        text, _ = bot.extract_pdf_text_and_images(file_bytes, max_images=0)
        return text or ""
    if fname.endswith(".docx") or "word" in mime:
        try:
            import docx  # python-docx
        except ImportError:
            raise RuntimeError("python-docx غير مثبّت (pip install python-docx)")
        d = docx.Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in d.paragraphs)
    # txt / md / csv-ish
    for enc in ("utf-8", "utf-16", "cp1256", "latin-1"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return file_bytes.decode("utf-8", errors="ignore")


async def _parse_questions_to_mcqs(raw_text: str, n_hint: int = 0):
    """Use Gemini to turn an existing question bank into the MCQ schema."""
    import bot
    raw_text = raw_text[:bot.MAX_PDF_CHARS]
    n_line = (f"There appear to be questions in here; extract them ALL "
              f"(do not invent extra ones).")
    prompt = f"""You are given a file that ALREADY CONTAINS exam questions
(possibly multiple choice, possibly with the correct answer marked, possibly
just a list of facts/questions).

{n_line}

Convert them into clean MCQs. Rules:
- 4 options A, B, C, D, exactly one correct.
- If the source marks/indicates the correct answer, USE it.
- If options are missing, write plausible ones and pick the correct answer
  from the source content.
- Keep the original language of each question.
- Do not duplicate questions.

Return a JSON array of objects with fields: q, a, b, c, d, answer
("answer" is one of "A","B","C","D").

SOURCE:
\"\"\"
{raw_text}
\"\"\""""
    r = await bot.call_gemini(model=bot.GEMINI_MODEL, contents=prompt,
                              config=bot._gen_config())
    return bot._parse_mcq_response(r)


async def _handle_qfile_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    uid = update.effective_user.id
    context.user_data.pop("mode", None)
    doc = update.message.document
    if doc is None:
        await update.message.reply_text("ابعت ملف PDF أو DOCX أو TXT فيه الأسئلة 📄")
        return
    if (doc.file_size or 0) > bot.MAX_FILE_SIZE:
        await update.message.reply_text("❌ الملف كبير جداً (الحد 20 ميجا).")
        return

    can, *_ , daily_limit = await db.can_consume(uid)
    if not can and not bot.is_admin(uid):
        await update.message.reply_text(
            f"❌ خلصت محاضراتك النهارده ({daily_limit}/يوم).",
            reply_markup=bot.get_keyboard_for(uid))
        return

    status = await update.message.reply_text("جاري قراءة ملف الأسئلة… ⏳")
    try:
        tg_file = await doc.get_file()
        fb = bytes(await tg_file.download_as_bytearray())
        text = await asyncio.to_thread(_extract_text_any, fb,
                                       doc.file_name or "", doc.mime_type or "")
    except Exception as e:
        await status.edit_text(f"❌ مقدرتش أقرأ الملف: {bot.html_escape(str(e))}",
                               parse_mode="HTML")
        return

    if not text or len(text.strip()) < 30:
        await status.edit_text("معرفتش ألاقي أسئلة كفاية في الملف ❌")
        return

    await status.edit_text("بحوّل الأسئلة لكويز تفاعلي… 🧠")
    try:
        mcqs = await _parse_questions_to_mcqs(text)
    except Exception as e:
        await status.edit_text(f"❌ خطأ في المعالجة: {bot.html_escape(str(e))}",
                               parse_mode="HTML")
        return

    if not mcqs:
        await status.edit_text("معرفتش أستخرج أسئلة من الملف ده ❌")
        return

    if not bot.is_admin(uid):
        await db.consume(uid)
    await status.edit_text(f"تمام، طلّعت {len(mcqs)} سؤال ✅")
    await bot.send_quizzes(update.message.chat, mcqs, uid)
    await db.log_generation(uid, len(mcqs), "qfile", "auto", "mixed", True)
    context.user_data[bot.LAST_MCQS_CACHE] = mcqs


# ============================================================
# Spot Diagnosis  (#6)
# ============================================================
async def _diagnose_image(img_bytes: bytes, mime: str) -> dict:
    import bot
    prompt = """Look at this image (likely a medical/clinical/anatomy/lab
image, but could be anything identifiable). Identify it precisely.

Return ONLY a JSON object:
{
  "title": "short label of what kind of image this is (e.g. 'Histology slide')",
  "answer": "the precise identification / diagnosis",
  "rationale": "1-3 short sentences on the key features that give it away"
}
Reply in the dominant language of any text in the image, else English."""
    r = await bot.call_gemini(
        model=bot.GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=img_bytes,
                                        mime_type=mime or "image/jpeg"), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.4,
            max_output_tokens=600,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    raw = (r.text or "").strip()
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"title": "Identification", "answer": raw or "—", "rationale": ""}


async def _handle_spot_image(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             img_bytes: bytes, mime: str):
    import bot
    uid = update.effective_user.id
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        d = await _diagnose_image(img_bytes, mime)
    except Exception as e:
        await update.message.reply_text(
            f"❌ مقدرتش أحلل الصورة: {bot.html_escape(str(e))}", parse_mode="HTML")
        return

    title = bot.html_escape(d.get("title", "تشخيص"))
    answer = bot.html_escape(d.get("answer", "—"))
    rationale = bot.html_escape(d.get("rationale", ""))

    # tg-spoiler hides the answer until tapped.
    caption = (
        f"🔍 <b>Spot Diagnosis</b>\n"
        f"النوع: {title}\n\n"
        f"الإجابة (اضغط للكشف): <span class=\"tg-spoiler\">{answer}</span>"
    )
    if rationale:
        caption += f"\n\n💡 <span class=\"tg-spoiler\">{rationale}</span>"

    try:
        await update.message.reply_photo(
            photo=io.BytesIO(img_bytes), caption=caption, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(caption, parse_mode="HTML")


async def cmd_spot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    context.user_data["mode"] = MODE_SPOT
    await update.message.reply_text(
        "🔍 <b>وضع Spot Diagnosis</b>\n\n"
        "ابعت صورة (أو أكتر) وأنا هحددلك إيه دي. "
        "الإجابة هتكون مخفية وتقدر تضغط عليها تكشفها.\n"
        "للخروج اضغط /start.",
        parse_mode="HTML")


# ============================================================
# High-priority interceptors (group -1)
# ============================================================
async def _intercept_documents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    uid = update.effective_user.id
    # Admin restore upload
    if (bot.is_admin(uid)
            and context.user_data.get("addon_action") == "awaiting_restore"):
        await _do_restore(update, context)
        raise ApplicationHandlerStop
    # Quiz-from-file mode
    if context.user_data.get("mode") == MODE_QFILE:
        if not await bot.ensure_access(update):
            raise ApplicationHandlerStop
        await track("qfile", _handle_qfile_document(update, context))
        raise ApplicationHandlerStop
    # Spot mode + document image
    if context.user_data.get("mode") == MODE_SPOT:
        doc = update.message.document
        mime = (doc.mime_type or "").lower() if doc else ""
        fname = (doc.file_name or "").lower() if doc else ""
        if mime.startswith("image/") or any(
                fname.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
            if not await bot.ensure_access(update):
                raise ApplicationHandlerStop
            tg_file = await doc.get_file()
            fb = bytes(await tg_file.download_as_bytearray())
            await track("spot", _handle_spot_image(update, context, fb,
                                                    mime or "image/jpeg"))
            raise ApplicationHandlerStop
    # otherwise let bot's own handlers run
    return


async def _intercept_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if context.user_data.get("mode") == MODE_SPOT:
        if not await bot.ensure_access(update):
            raise ApplicationHandlerStop
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        fb = bytes(await tg_file.download_as_bytearray())
        await track("spot", _handle_spot_image(update, context, fb, "image/jpeg"))
        raise ApplicationHandlerStop
    return


# ============================================================
# Registration
# ============================================================
def register(app: Application):
    # Commands
    app.add_handler(CommandHandler("backup",    cmd_backup))
    app.add_handler(CommandHandler("restore",   cmd_restore))
    app.add_handler(CommandHandler("smartdash", cmd_smartdash))
    app.add_handler(CommandHandler("spot",      cmd_spot))
    app.add_handler(CommandHandler("qfile",     _cmd_qfile))
    app.add_handler(CommandHandler("tasks",     cmd_tasks))
    app.add_handler(CommandHandler("health",    cmd_health))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_smartdash_cb, pattern=r"^addon:sm:"))

    # High-priority interceptors (run before bot's own doc/photo handlers).
    app.add_handler(MessageHandler(filters.Document.ALL, _intercept_documents),
                    group=-1)
    app.add_handler(MessageHandler(filters.PHOTO, _intercept_photos), group=-1)

    logger.info("addons registered")


async def _cmd_qfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    context.user_data["mode"] = MODE_QFILE
    await update.message.reply_text(
        "📋 <b>كويز من ملف أسئلة</b>\n\n"
        "ابعت ملف فيه أسئلة (PDF / DOCX / TXT) وأنا هحوّلها لكويز جاهز.\n"
        "للخروج اضغط /start.",
        parse_mode="HTML")
