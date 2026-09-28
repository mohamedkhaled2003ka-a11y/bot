"""
medical.py — Medical file analysis & corrected Spot Diagnosis (#5, #6)
=====================================================================
DROP-IN, same pattern as addons.py: registers high-priority interceptors
(group -1) and uses ApplicationHandlerStop so your normal MCQ flow is
untouched while the user is in diagnosis mode.

What changed vs the old addons.cmd_spot:
  OLD behaviour was a quiz-style "guess what this is", hiding the answer in a
  tg-spoiler. That is the REVERSED behaviour the user reported.
  NEW behaviour: the user uploads a medical image / scan / lab report / PDF,
  the bot ANALYZES it and returns findings + impression openly. For PDFs it
  extracts BOTH the text and any embedded images and feeds them to the model.

Wiring (in bot.py main(), after build):
    import medical
    medical.register(app)

Then add a "🩺 التشخيص الطبي" button that calls /diagnose (cmd_diagnose).

NOTE: this is decision-support for study/triage, NOT a substitute for a
licensed clinician. The disclaimer is shown to the user every time.
"""
from __future__ import annotations

import io
import asyncio
import logging

import fitz  # PyMuPDF
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ApplicationHandlerStop, filters,
)

logger = logging.getLogger("medical")

MODE_MED = "med_diag"
MAX_PDF_IMAGES = 12
MIN_IMG_BYTES = 6000

DISCLAIMER = ("⚠️ <i>تنبيه: التحليل ده لأغراض تعليمية ومساعدة المذاكرة فقط، "
              "ومش بديل عن طبيب مختص.</i>")


# ============================================================ extraction
def _extract_pdf(pdf_bytes: bytes) -> tuple[str, list[tuple[bytes, str]]]:
    """Return (text, [(image_bytes, mime), ...]) from a PDF."""
    text_parts: list[str] = []
    images: list[tuple[bytes, str]] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            t = page.get_text("text") or ""
            if t.strip():
                text_parts.append(t)
            for img in page.get_images(full=True):
                if len(images) >= MAX_PDF_IMAGES:
                    break
                xref = img[0]
                try:
                    base = doc.extract_image(xref)
                    data = base.get("image")
                    ext = (base.get("ext") or "png").lower()
                    if data and len(data) >= MIN_IMG_BYTES:
                        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                        images.append((data, mime))
                except Exception:
                    continue
        # If the PDF is a scan (no text, no extractable images), rasterize pages
        if not text_parts and not images:
            for page in doc:
                if len(images) >= MAX_PDF_IMAGES:
                    break
                pix = page.get_pixmap(dpi=150)
                images.append((pix.tobytes("png"), "image/png"))
    finally:
        doc.close()
    return ("\n".join(text_parts), images)


# ============================================================ analysis
_PROMPT = """You are a careful medical study assistant. Analyze the supplied
material (which may be a clinical image, radiology scan, histology slide, lab
report, or a document with text and figures). Produce a clear, structured
analysis IN THE DOMINANT LANGUAGE of the material (Arabic if the text is
Arabic, else English).

Return ONLY this JSON object:
{
  "modality": "what kind of material this is (e.g. 'Chest X-ray', 'CBC lab report', 'Histology H&E')",
  "findings": ["specific observation 1", "observation 2", "..."],
  "impression": "the most likely interpretation / leading diagnosis in 1-3 sentences",
  "differential": ["alternative 1", "alternative 2"],
  "recommendation": "suggested next step for a student/clinician (1 sentence)"
}
Be specific and base every statement on what is actually present. If something
cannot be determined from the material, say so rather than guessing."""


async def _analyze(parts: list, extra_text: str = "") -> dict:
    """parts: list of google.genai types.Part (+ trailing prompt str)."""
    import bot
    from google.genai import types
    content = list(parts)
    prompt = _PROMPT
    if extra_text.strip():
        prompt += "\n\nExtracted text from the document:\n\"\"\"\n" \
                  + extra_text[:12000] + "\n\"\"\""
    content.append(prompt)
    r = await bot.call_gemini(
        model=bot.GEMINI_MODEL, contents=content,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3, max_output_tokens=1200,
        ),
    )
    import json
    raw = (r.text or "").strip()
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"modality": "Analysis", "impression": raw or "—",
            "findings": [], "differential": [], "recommendation": ""}


def _format(d: dict) -> str:
    import bot
    esc = bot.html_escape
    out = [f"🩺 <b>{esc(d.get('modality', 'تحليل'))}</b>", ""]
    findings = d.get("findings") or []
    if findings:
        out.append("<b>الملاحظات / Findings:</b>")
        out += [f"• {esc(x)}" for x in findings]
        out.append("")
    if d.get("impression"):
        out.append(f"<b>الانطباع / Impression:</b>\n{esc(d['impression'])}\n")
    diff = d.get("differential") or []
    if diff:
        out.append("<b>تشخيصات تفريقية / Differential:</b>")
        out += [f"• {esc(x)}" for x in diff]
        out.append("")
    if d.get("recommendation"):
        out.append(f"<b>التوصية / Next step:</b>\n{esc(d['recommendation'])}\n")
    out.append(DISCLAIMER)
    return "\n".join(out)


# ============================================================ handlers
async def _run_image(update, context, img_bytes: bytes, mime: str):
    import bot
    from google.genai import types
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        parts = [types.Part.from_bytes(data=img_bytes, mime_type=mime or "image/jpeg")]
        d = await _analyze(parts)
    except Exception as e:
        await update.message.reply_text(
            f"❌ مقدرتش أحلل الصورة: {bot.html_escape(str(e))}", parse_mode="HTML")
        return
    await update.message.reply_text(_format(d), parse_mode="HTML")


async def _run_pdf(update, context, pdf_bytes: bytes):
    import bot
    from google.genai import types
    status = await update.message.reply_text("📄 بقرأ الملف وأستخرج الصور… ⏳")
    try:
        text, images = await asyncio.to_thread(_extract_pdf, pdf_bytes)
    except Exception as e:
        await status.edit_text(f"❌ مقدرتش أقرأ الـ PDF: {bot.html_escape(str(e))}",
                               parse_mode="HTML")
        return
    if not text.strip() and not images:
        await status.edit_text("ملقيتش محتوى أقدر أحلله في الملف ده ❌")
        return
    await status.edit_text(f"🔬 بحلل المحتوى ({len(images)} صورة)… 🧠")
    parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in images]
    try:
        d = await _analyze(parts, extra_text=text)
    except Exception as e:
        await status.edit_text(f"❌ خطأ في التحليل: {bot.html_escape(str(e))}",
                               parse_mode="HTML")
        return
    await status.edit_text(_format(d), parse_mode="HTML")


async def cmd_diagnose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    context.user_data["mode"] = MODE_MED
    await update.message.reply_text(
        "🩺 <b>التشخيص الطبي / تحليل الملفات</b>\n\n"
        "ابعت <b>صورة</b> طبية، <b>أشعة</b>، <b>تحليل معملي</b>، أو ملف "
        "<b>PDF</b> (هستخرج منه النص والصور تلقائياً) وأنا هحلّله وأديك "
        "الملاحظات والتشخيص المبدئي.\n"
        "للخروج اضغط /start.\n\n" + DISCLAIMER,
        parse_mode="HTML")


# -------- interceptors (group -1) --------
async def _intercept_documents(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if context.user_data.get("mode") != MODE_MED:
        return
    doc = update.message.document
    if doc is None:
        return
    if not await bot.ensure_access(update):
        raise ApplicationHandlerStop
    if (doc.file_size or 0) > bot.MAX_FILE_SIZE:
        await update.message.reply_text("❌ الملف كبير جداً (الحد 20 ميجا).")
        raise ApplicationHandlerStop
    mime = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()
    tg_file = await doc.get_file()
    fb = bytes(await tg_file.download_as_bytearray())
    import addons  # reuse the tracked-task helper
    if mime == "application/pdf" or fname.endswith(".pdf"):
        await addons.track("med-pdf", _run_pdf(update, context, fb))
    elif mime.startswith("image/") or any(
            fname.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
        await addons.track("med-img", _run_image(update, context, fb,
                                                  mime or "image/jpeg"))
    else:
        await update.message.reply_text("ابعت صورة طبية أو ملف PDF 📄")
    raise ApplicationHandlerStop


async def _intercept_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot, addons
    if context.user_data.get("mode") != MODE_MED:
        return
    if not await bot.ensure_access(update):
        raise ApplicationHandlerStop
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    fb = bytes(await tg_file.download_as_bytearray())
    await addons.track("med-img", _run_image(update, context, fb, "image/jpeg"))
    raise ApplicationHandlerStop


def register(app: Application):
    app.add_handler(CommandHandler("diagnose", cmd_diagnose))
    # group -2 so it runs BEFORE addons' own (-1) interceptors
    app.add_handler(MessageHandler(filters.Document.ALL, _intercept_documents),
                    group=-2)
    app.add_handler(MessageHandler(filters.PHOTO, _intercept_photos), group=-2)
    logger.info("medical registered")
