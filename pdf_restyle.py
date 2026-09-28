"""AI-assisted PDF organization and restyling flow."""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, HRFlowable
from telegram import InputFile, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes, MessageHandler, filters

import qexport
import db

logger = logging.getLogger("pdf_restyle")
MODE_UPLOAD = "pdf_restyle_upload"
MODE_PROMPT = "pdf_restyle_prompt"
MAX_PROMPT_LENGTH = 1800

_RESTYLE_SCHEMA = {
    "type": "object",
    "required": ["title", "subtitle", "summary", "sections"],
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "summary": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["heading", "paragraphs", "bullets"],
                "properties": {
                    "heading": {"type": "string"},
                    "paragraphs": {"type": "array", "items": {"type": "string"}},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


def _clean_json(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ValueError("Gemini returned an invalid document structure")
    return data


def _safe(value, limit: int = 1800) -> str:
    return str(value or "").strip()[:limit]


async def cmd_restyle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    uid = update.effective_user.id
    if not bot.is_admin(uid) and not bot._PDF_RESTYLE_ENABLED:
        await update.message.reply_text("ميزة إعادة تنسيق PDF متوقفة حالياً من الأدمن.")
        return
    balance = await db.get_pdf_tokens(uid)
    token_line = "استخدام مجاني للأدمن." if bot.is_admin(uid) else f"رصيدك: {balance} token | تكلفة العملية: {bot._PDF_RESTYLE_COST}"
    context.user_data["mode"] = MODE_UPLOAD
    context.user_data.pop("restyle_pdf_text", None)
    context.user_data.pop("restyle_pdf_images", None)
    await update.message.reply_text(
        "✨ <b>إعادة تنسيق PDF بالذكاء الاصطناعي</b>\n\n"
        "ابعت ملف PDF، وبعدها اكتب وصفك للتنسيق والتنظيم المطلوب.\n"
        "مثال: اجعله مذكرة دراسية واضحة بعناوين وفواصل وملخص في البداية.\n\n"
        f"🎟️ {token_line}",
        parse_mode="HTML",
    )


async def _button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_restyle(update, context)
    raise ApplicationHandlerStop


async def _intercept_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if context.user_data.get("mode") != MODE_UPLOAD:
        return
    if not await bot.ensure_access(update):
        raise ApplicationHandlerStop
    doc = update.message.document
    fname = (doc.file_name or "").lower()
    if (doc.mime_type or "").lower() != "application/pdf" and not fname.endswith(".pdf"):
        await update.message.reply_text("ابعت ملف PDF فقط في الوضع ده 📄")
        raise ApplicationHandlerStop
    if (doc.file_size or 0) > bot.MAX_FILE_SIZE:
        await update.message.reply_text("❌ الملف كبير جداً (الحد 20 ميجا).")
        raise ApplicationHandlerStop

    status = await update.message.reply_text("جاري قراءة الـPDF وتجهيز المحتوى… ⏳")
    try:
        tg_file = await doc.get_file()
        pdf_bytes = bytes(await tg_file.download_as_bytearray())
        text, images = await asyncio.to_thread(bot.extract_pdf_text_and_images, pdf_bytes)
    except Exception:
        logger.exception("PDF restyle upload failed")
        await status.edit_text("حصل خطأ في قراءة الملف ❌")
        raise ApplicationHandlerStop
    if not text.strip() and not images:
        await status.edit_text("معرفتش أستخرج محتوى من الملف ده ❌")
        raise ApplicationHandlerStop

    context.user_data["restyle_pdf_text"] = text[:bot.MAX_PDF_CHARS]
    context.user_data["restyle_pdf_images"] = images
    context.user_data["mode"] = MODE_PROMPT
    await status.edit_text(
        "تمت قراءة الملف ✅\n\n"
        "اكتب دلوقتي وصف الشكل والتنظيم اللي عايزه، أو اكتب: <b>رتبه بشكل احترافي</b>.",
        parse_mode="HTML",
    )
    raise ApplicationHandlerStop


async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    import bot
    prompt = (prompt or "").strip()
    if not prompt:
        await update.message.reply_text("اكتب وصف التنسيق المطلوب أو /cancel للإلغاء.")
        return
    if len(prompt) > MAX_PROMPT_LENGTH:
        await update.message.reply_text(f"الوصف طويل جداً. الحد الأقصى {MAX_PROMPT_LENGTH} حرف.")
        return
    if not context.user_data.get("restyle_pdf_text") and not context.user_data.get("restyle_pdf_images"):
        context.user_data["mode"] = MODE_UPLOAD
        await update.message.reply_text("الجلسة انتهت. ابعت ملف PDF جديد الأول.")
        return
    uid = update.effective_user.id
    consumed = False
    if not bot.is_admin(uid):
        consumed, balance = await db.consume_pdf_tokens(uid, bot._PDF_RESTYLE_COST)
        if not consumed:
            await update.message.reply_text(
                f"❌ رصيدك غير كافٍ. تحتاج {bot._PDF_RESTYLE_COST} token، والمتاح {balance}.\n"
                "اطلب من الأدمن إضافة رصيد.")
            return
    context.user_data["mode"] = None
    await _generate_and_send(update, context, prompt, consumed_amount=(bot._PDF_RESTYLE_COST if consumed else 0))


async def _generate_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, user_prompt: str, consumed_amount: int = 0):
    import bot
    text = context.user_data.get("restyle_pdf_text", "")
    images = context.user_data.get("restyle_pdf_images", []) or []
    status = await update.message.reply_text("جاري إعادة تنظيم وتصميم الملف… ⏳")
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    instruction = (
        "You are an expert document editor. Reorganize the source into a clean, readable PDF. "
        "Preserve factual meaning and do not invent information. Follow the user's design request. "
        "Return JSON only with title, subtitle, summary, and sections. Each section has heading, "
        "paragraphs, and bullets. Keep the source language, and use concise paragraphs.\n\n"
        f"USER DESIGN REQUEST:\n{user_prompt}\n\nSOURCE TEXT:\n{text[:bot.MAX_PDF_CHARS]}"
    )
    contents = []
    for image_bytes, mime in images[:bot.MAX_IMAGES_PER_PDF]:
        contents.append(bot.types.Part.from_bytes(data=image_bytes, mime_type=mime))
    contents.append(instruction)
    try:
        response = await bot.call_gemini(
            model=bot.GEMINI_MODEL,
            contents=contents,
            config=bot.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_RESTYLE_SCHEMA,
                temperature=0.35,
                max_output_tokens=bot.MAX_OUTPUT_TOKENS,
                thinking_config=bot.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        document = _clean_json(response.text or "")
        pdf_bytes = await asyncio.to_thread(_render_pdf, document, images)
    except Exception as exc:
        logger.exception("PDF restyle failed")
        if consumed_amount:
            await db.add_pdf_tokens(update.effective_user.id, consumed_amount)
        await status.edit_text(
            f"حصل خطأ في إعادة التنسيق ❌\n<code>{bot.html_escape(type(exc).__name__)}</code>",
            parse_mode="HTML",
        )
        context.user_data.pop("restyle_pdf_text", None)
        context.user_data.pop("restyle_pdf_images", None)
        return

    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf_bytes), filename="restyled_document.pdf"),
        caption="✅ اتفضل، ده الـPDF بعد إعادة التنظيم والتنسيق.",
    )
    await status.delete()
    context.user_data.pop("restyle_pdf_text", None)
    context.user_data.pop("restyle_pdf_images", None)


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    lines = html.escape(_safe(text)).splitlines() or [""]
    shaped = "<br/>".join(qexport.shape(line) for line in lines)
    return Paragraph(shaped, style)


def _render_pdf(document: dict, images: list[tuple[bytes, str]]) -> bytes:
    qexport._register_font(None)
    ink = colors.HexColor("#19333D")
    muted = colors.HexColor("#61767C")
    accent = colors.HexColor("#0E7490")
    body = ParagraphStyle("restyle_body", fontName=qexport.FONT_NAME, fontSize=10.5, leading=18, alignment=TA_RIGHT, textColor=ink, spaceAfter=7)
    heading = ParagraphStyle("restyle_heading", parent=body, fontSize=15, leading=22, textColor=accent, spaceBefore=14, spaceAfter=7, borderPadding=5)
    title = ParagraphStyle("restyle_title", parent=body, fontSize=25, leading=33, alignment=TA_CENTER, textColor=colors.HexColor("#12343B"), spaceAfter=8)
    subtitle = ParagraphStyle("restyle_subtitle", parent=body, fontSize=11, leading=18, alignment=TA_CENTER, textColor=muted, spaceAfter=18)
    summary = ParagraphStyle("restyle_summary", parent=body, backColor=colors.HexColor("#F0F7F8"), borderColor=colors.HexColor("#B9D9DE"), borderWidth=0.7, borderPadding=11, spaceAfter=16)
    summary_label = ParagraphStyle("restyle_summary_label", parent=body, fontSize=9, leading=13, textColor=accent, spaceAfter=4)
    bullet = ParagraphStyle("restyle_bullet", parent=body, leftIndent=14, firstLineIndent=-10, spaceAfter=4)
    label = ParagraphStyle("restyle_label", parent=body, fontSize=9, leading=12, alignment=TA_CENTER, textColor=accent, spaceAfter=22)

    story = [Spacer(1, 42 * mm), _p("AI DOCUMENT STUDIO", label),
             _p(document.get("title", "Restyled document"), title)]
    if document.get("subtitle"):
        story.append(_p(document["subtitle"], subtitle))
    if document.get("summary"):
        story.append(_p("الملخص", summary_label))
        story.append(_p(_safe(document["summary"], 3200), summary))

    story.append(PageBreak())

    for section_number, section in enumerate(document.get("sections", [])[:40], 1):
        if section.get("heading"):
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#D5E5E7"), spaceBefore=8, spaceAfter=2))
            story.append(_p(f"{section_number:02d}  {section['heading']}", heading))
        for paragraph in section.get("paragraphs", [])[:12]:
            story.append(_p(paragraph, body))
        for item in section.get("bullets", [])[:20]:
            story.append(_p("• " + _safe(item), bullet))

    for image_bytes, _mime in images[:8]:
        try:
            image = Image(io.BytesIO(image_bytes))
            image._restrictSize(165 * mm, 90 * mm)
            story.extend([Spacer(1, 8), image])
        except Exception:
            logger.debug("Skipping an embedded image during PDF rendering", exc_info=True)

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm, topMargin=23 * mm, bottomMargin=20 * mm, title="Restyled document")

    def draw_page_chrome(canvas, _doc):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(colors.HexColor("#B9D9DE"))
        canvas.setLineWidth(0.7)
        canvas.line(20 * mm, height - 17 * mm, width - 20 * mm, height - 17 * mm)
        canvas.setFillColor(colors.HexColor("#0E7490"))
        canvas.rect(20 * mm, height - 17.7 * mm, 18 * mm, 1.4 * mm, stroke=0, fill=1)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#71858A"))
        canvas.drawString(20 * mm, 11 * mm, "AI DOCUMENT STUDIO")
        canvas.drawRightString(width - 20 * mm, 11 * mm, f"{canvas.getPageNumber():02d}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page_chrome, onLaterPages=draw_page_chrome)
    return output.getvalue()


def register(app: Application):
    app.add_handler(CommandHandler("restyle", cmd_restyle))
    app.add_handler(
        MessageHandler(filters.Regex(r"^✨\s*إعادة تنسيق PDF$"), _button_handler),
        group=-5,
    )
    app.add_handler(MessageHandler(filters.Document.ALL, _intercept_document), group=-4)
    logger.info("pdf_restyle registered")
