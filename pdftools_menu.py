"""
pdftools_menu.py — Telegram UI for the PDF Tools suite (#3)
==========================================================
DROP-IN. Uses pdftools.py as the engine. Registers a /pdftools command and a
high-priority document/photo interceptor (group -1) that only acts while the
user is in a pdf-tool mode, so your MCQ flow is untouched.

Wiring (bot.py main()):  import pdftools_menu ; pdftools_menu.register(app)
Add a "🛠 أدوات PDF" button that calls cmd_pdftools.
"""
from __future__ import annotations

import io
import asyncio
import logging

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ApplicationHandlerStop, filters,
)

import pdftools

logger = logging.getLogger("pdftools_menu")

MODE = "pdftool"

# op -> (label, needs_office)
OPS = {
    "img2pdf":  ("🖼️ صور → PDF", False),
    "pdf2img":  ("🖼️ PDF → صور", False),
    "merge":    ("📎 دمج PDF", False),
    "split":    ("✂️ تقسيم PDF", False),
    "compress": ("🗜️ ضغط PDF", False),
    "pdf2word": ("📄 PDF → Word", False),
    "word2pdf": ("📄 Word → PDF", True),
    "ppt2pdf":  ("📊 PowerPoint → PDF", True),
    "pdf2ppt":  ("📊 PDF → PowerPoint", False),
}


def _menu() -> InlineKeyboardMarkup:
    office = pdftools.soffice_available()
    rows, row = [], []
    for op, (label, needs) in OPS.items():
        button_label = label
        if needs and not office:
            button_label += " ⚠️"
        row.append(InlineKeyboardButton(button_label, callback_data=f"pdft:op:{op}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if not office:
        rows.append([InlineKeyboardButton(
            "ℹ️ Word/PPT → PDF تحتاج LibreOffice",
            callback_data="pdft:noop")])
    return InlineKeyboardMarkup(rows)


async def cmd_pdftools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    context.user_data.pop(MODE, None)
    context.user_data.pop("pdftool_op", None)
    context.user_data.pop("pdftool_files", None)
    await update.message.reply_text(
        "🛠 <b>أدوات PDF</b>\n\nاختار العملية اللي عايزها:",
        parse_mode="HTML", reply_markup=_menu())


_PROMPTS = {
    "img2pdf":  "ابعت صورة واحدة أو أكتر، وبعدين اضغط «تم» للتحويل لـ PDF.",
    "pdf2img":  "ابعت ملف PDF وهرجّعهولك صور.",
    "merge":    "ابعت ملفات PDF واحد ورا التاني، وبعدين اضغط «تم» للدمج.",
    "split":    "ابعت ملف PDF وهقسّمه صفحة صفحة.",
    "compress": "ابعت ملف PDF وهضغطه.",
    "pdf2word": "ابعت ملف PDF وهحوّله Word.",
    "word2pdf": "ابعت ملف Word (.docx) وهحوّله PDF.",
    "ppt2pdf":  "ابعت ملف PowerPoint (.pptx) وهحوّله PDF.",
    "pdf2ppt":  "ابعت ملف PDF وهحوّله PowerPoint.",
}
_MULTI = {"img2pdf", "merge"}


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    q = update.callback_query
    await q.answer()
    if not await bot.ensure_access(update):
        return
    parts = q.data.split(":")
    if q.data == "pdft:noop":
        return
    if parts[1] == "op":
        op = parts[2]
        if OPS[op][1] and not pdftools.soffice_available():
            await q.answer(
                "ثبّت LibreOffice أولاً لتفعيل تحويل Word/PowerPoint إلى PDF.",
                show_alert=True,
            )
            return
        context.user_data[MODE] = True
        context.user_data["pdftool_op"] = op
        context.user_data["pdftool_files"] = []
        extra = ""
        kb = None
        if op in _MULTI:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "✅ تم — نفّذ", callback_data="pdft:done")]])
        await bot.safe_edit(q, f"📥 {_PROMPTS[op]}\n\n/start للإلغاء.",
                            reply_markup=kb)
        return
    if q.data == "pdft:done":
        await _finish_multi(q, context)
        return


async def _download(update) -> tuple[bytes, str, str]:
    """Return (bytes, filename, mime) from a document or photo message."""
    msg = update.message
    if msg.document:
        f = await msg.document.get_file()
        return (bytes(await f.download_as_bytearray()),
                msg.document.file_name or "file",
                (msg.document.mime_type or "").lower())
    if msg.photo:
        f = await msg.photo[-1].get_file()
        return bytes(await f.download_as_bytearray()), "photo.jpg", "image/jpeg"
    return b"", "", ""


async def _intercept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not context.user_data.get(MODE):
        return
    op = context.user_data.get("pdftool_op")
    if not op:
        return
    if not await bot.ensure_access(update):
        raise ApplicationHandlerStop
    data, fname, mime = await _download(update)
    if not data:
        raise ApplicationHandlerStop
    if len(data) > bot.MAX_FILE_SIZE:
        await update.message.reply_text("❌ الملف كبير جداً (الحد 20 ميجا).")
        raise ApplicationHandlerStop

    if op in _MULTI:
        context.user_data.setdefault("pdftool_files", []).append((data, fname, mime))
        n = len(context.user_data["pdftool_files"])
        await update.message.reply_text(
            f"📥 استلمت {n} ملف. ابعت كمان أو اضغط «تم».",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "✅ تم — نفّذ", callback_data="pdft:done")]]))
        raise ApplicationHandlerStop

    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    import addons
    await addons.track(f"pdftool:{op}",
                       _run_single(update, context, op, data, fname, mime))
    raise ApplicationHandlerStop


async def _run_single(update, context, op, data, fname, mime):
    import bot
    try:
        result = await asyncio.to_thread(_do_op, op, [(data, fname, mime)])
    except Exception as e:
        await update.message.reply_text(
            f"❌ فشل التحويل: {bot.html_escape(str(e))}", parse_mode="HTML")
        return
    await _send_result(update, op, result)
    context.user_data.pop(MODE, None)


async def _finish_multi(q, context):
    import bot, addons
    files = context.user_data.get("pdftool_files") or []
    op = context.user_data.get("pdftool_op")
    if not files:
        await q.answer("مفيش ملفات لسه", show_alert=True)
        return
    await bot.safe_edit(q, f"⏳ بنفّذ على {len(files)} ملف…")
    await addons.track(f"pdftool:{op}",
                       _run_multi(q, context, op, files))


async def _run_multi(q, context, op, files):
    import bot
    try:
        result = await asyncio.to_thread(_do_op, op, files)
    except Exception as e:
        await q.message.reply_text(
            f"❌ فشل التنفيذ: {bot.html_escape(str(e))}", parse_mode="HTML")
        return

    class _Shim:  # minimal adapter so _send_result can reply
        message = q.message
    await _send_result(_Shim(), op, result)
    context.user_data.pop(MODE, None)
    context.user_data.pop("pdftool_files", None)


def _do_op(op, files):
    """Synchronous engine dispatch. Returns either bytes or list[(name,bytes)]."""
    blobs = [f[0] for f in files]
    if op == "img2pdf":
        return ("output.pdf", pdftools.images_to_pdf(blobs))
    if op == "pdf2img":
        imgs = pdftools.pdf_to_images(blobs[0])
        return [(f"page_{i+1:03d}.png", b) for i, b in enumerate(imgs)]
    if op == "merge":
        return ("merged.pdf", pdftools.merge_pdfs(blobs))
    if op == "split":
        return pdftools.split_pdf(blobs[0])
    if op == "compress":
        return ("compressed.pdf", pdftools.compress_pdf(blobs[0]))
    if op == "pdf2word":
        return ("output.docx", pdftools.pdf_to_word(blobs[0]))
    if op == "word2pdf":
        return ("output.pdf", pdftools.office_to_pdf(blobs[0], "docx"))
    if op == "ppt2pdf":
        return ("output.pdf", pdftools.office_to_pdf(blobs[0], "pptx"))
    if op == "pdf2ppt":
        return ("output.pptx", pdftools.pdf_to_office(blobs[0], "pptx"))
    raise ValueError(f"unknown op {op}")


async def _send_result(update, op, result):
    msg = update.message
    if isinstance(result, tuple):
        name, data = result
        await msg.reply_document(
            document=InputFile(io.BytesIO(data), filename=name),
            caption="✅ اتفضّل")
        return
    # list of files (split / pdf2img) — send up to a sane cap
    items = result[:60]
    for name, data in items:
        await msg.reply_document(
            document=InputFile(io.BytesIO(data), filename=name))
    if len(result) > len(items):
        await msg.reply_text(f"(تم إرسال أول {len(items)} ملف فقط)")
    else:
        await msg.reply_text("✅ خلصت")


def register(app: Application):
    app.add_handler(CommandHandler("pdftools", cmd_pdftools))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^pdft:"))
    app.add_handler(MessageHandler(filters.Document.ALL, _intercept), group=-3)
    app.add_handler(MessageHandler(filters.PHOTO, _intercept), group=-3)
    logger.info("pdftools_menu registered")
