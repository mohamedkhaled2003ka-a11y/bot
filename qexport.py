"""
qexport.py — export generated MCQs to a clean, Arabic-correct PDF.
================================================================
DROP-IN. Pure Python (reportlab + arabic-reshaper + python-bidi).
No system packages, no LibreOffice. Works on any host.

Why your old PDF export showed broken Arabic:
  PDFs are not HTML. Arabic in a PDF must be (1) shaped — letters joined into
  their initial/medial/final forms — and (2) reordered for right-to-left with
  the bidi algorithm BEFORE you draw the glyphs. reportlab does neither on its
  own, and the built-in fonts have no Arabic glyphs at all. This module does
  both and embeds a real Arabic font.

Public API:
    build_quiz_pdf(mcqs, *, title="أسئلة الاختيار من متعدد",
                   with_answers=True, font_path=None) -> bytes
    where each mcq is the same dict your bot already produces:
        {"question": str, "options": [str, str, ...],
         "correct_index": int, "explanation": str (optional)}
"""
from __future__ import annotations

import os
import re
from io import BytesIO

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER

FONT_NAME = "ArBody"

# Look for a bundled font next to this file (fonts/), then common system paths.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_CANDIDATES = [
    os.path.join(_HERE, "fonts", "Amiri-Regular.ttf"),
    os.path.join(_HERE, "fonts", "NotoNaskhArabic-Regular.ttf"),
    os.path.join(_HERE, "Amiri-Regular.ttf"),
    "/usr/share/fonts/truetype/amiri/Amiri-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def _resolve_font(font_path: str | None) -> str:
    cands = ([font_path] if font_path else []) + _FONT_CANDIDATES
    for p in cands:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "No Arabic TTF found. Put Amiri-Regular.ttf in a 'fonts/' folder next "
        "to qexport.py, or pass font_path=...")


def _register_font(font_path: str | None):
    path = _resolve_font(font_path)
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT_NAME, path))


def shape(text: str) -> str:
    """Shape + bidi-reorder a string so it renders correctly in a PDF.
    Mixed Arabic/Latin/numbers are handled by the bidi algorithm."""
    if text is None:
        return ""
    text = str(text)
    if not _ARABIC_RE.search(text):
        return text  # pure latin/numeric: leave as-is
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


def _styles():
    base = ParagraphStyle(
        "ar", fontName=FONT_NAME, fontSize=13, leading=22,
        alignment=TA_RIGHT, wordWrap="RTL",
    )
    return {
        "title": ParagraphStyle("t", parent=base, fontSize=20, leading=30,
                                 alignment=TA_CENTER, spaceAfter=14,
                                 textColor=colors.HexColor("#1a5276")),
        "q": ParagraphStyle("q", parent=base, fontSize=14, leading=24,
                            spaceBefore=10, spaceAfter=6,
                            textColor=colors.HexColor("#154360")),
        "opt": ParagraphStyle("o", parent=base, leftIndent=6, rightIndent=6),
        "opt_correct": ParagraphStyle("oc", parent=base, leftIndent=6,
                                      rightIndent=6,
                                      textColor=colors.HexColor("#1e8449")),
        "expl": ParagraphStyle("e", parent=base, fontSize=11, leading=18,
                               textColor=colors.HexColor("#7d6608")),
    }


_AR_LETTERS = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح"]


def _mark(line: str, is_correct: bool) -> str:
    """Shape the option line. For the correct answer we append a font-safe
    textual marker instead of a ✓ glyph (many Arabic fonts lack ✓)."""
    if is_correct:
        # strip the placeholder ✔ and add a guaranteed-present marker
        line = line.replace("   ✔", "") + "  (الإجابة الصحيحة)"
    return shape(line)


def build_quiz_pdf(mcqs, *, title="أسئلة الاختيار من متعدد",
                   with_answers=True, font_path=None) -> bytes:
    _register_font(font_path)
    st = _styles()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=18 * mm, leftMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="Quiz",
    )
    story = [Paragraph(shape(title), st["title"]), Spacer(1, 6)]

    for i, m in enumerate(mcqs, 1):
        qtext = f"{i}. {m.get('question', '')}"
        story.append(Paragraph(shape(qtext), st["q"]))
        opts = m.get("options", []) or []
        correct = m.get("correct_index", -1)
        for j, opt in enumerate(opts):
            letter = _AR_LETTERS[j] if j < len(_AR_LETTERS) else str(j + 1)
            is_correct = with_answers and j == correct
            style = st["opt_correct"] if is_correct else st["opt"]
            line = f"({letter}) {opt}"
            if is_correct:
                line += "   ✔"  # rendered via fallback below
            story.append(Paragraph(_mark(line, is_correct), style))
        if with_answers and m.get("explanation"):
            story.append(Paragraph(shape("التعليل: " + m["explanation"]),
                                   st["expl"]))
        story.append(Spacer(1, 6))

    doc.build(story)
    return buf.getvalue()
