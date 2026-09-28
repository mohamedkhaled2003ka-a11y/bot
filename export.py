"""
export.py — PDF and Anki (.apkg) export of MCQs
================================================

Two entry points:
    generate_pdf(mcqs, title=...)  -> bytes
    generate_anki(mcqs, deck_name=...) -> bytes

Arabic support
--------------
Arabic letters must be (a) reshaped into their connected forms and
(b) reordered right-to-left for visual display in PDFs. We use
`arabic-reshaper` + `python-bidi` for this. For Anki we don't need
to reshape, because Anki renders text via the OS browser engine which
handles RTL natively — we just emit `<div dir="auto">`.

PDF font
--------
fpdf2 needs a Unicode TTF font to render Arabic. We expect an Arabic-
capable TTF at the path in env var `ARABIC_FONT_PATH`, default
`fonts/Amiri-Regular.ttf`. Amiri is free (OFL) — download once:
    https://fonts.google.com/specimen/Amiri
If the font is missing, Arabic content falls back to Latin transliteration
warnings (we degrade gracefully rather than crash).
"""

from __future__ import annotations

import io
import os
import random
import tempfile
from pathlib import Path
from typing import Iterable

import arabic_reshaper
from bidi.algorithm import get_display
from fpdf import FPDF
import genanki

ARABIC_FONT_PATH = Path(os.environ.get("ARABIC_FONT_PATH", "fonts/Amiri-Regular.ttf"))


# ─────────────────────────────────────────────────────────────
# Arabic shaping
# ─────────────────────────────────────────────────────────────
def _has_arabic(text: str) -> bool:
    return any('\u0600' <= ch <= '\u06FF' or '\u0750' <= ch <= '\u077F' for ch in text)


def _shape(text: str) -> str:
    """Reshape + bidi for proper RTL rendering inside fpdf2 text cells."""
    if not text:
        return ""
    if not _has_arabic(text):
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


# ─────────────────────────────────────────────────────────────
# PDF export
# ─────────────────────────────────────────────────────────────
class _PDF(FPDF):
    def __init__(self, font_family: str, has_rtl: bool):
        super().__init__(format="A4", unit="mm")
        self.set_auto_page_break(auto=True, margin=15)
        self.font_family = font_family
        self.has_rtl = has_rtl

    def header(self):
        pass

    def footer(self):
        self.set_y(-12)
        self.set_font(self.font_family, "", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)


def _add_unicode_font(pdf: FPDF) -> tuple[str, bool]:
    """Try to load Amiri (or the configured Arabic font).
    Returns (font_family_name, has_unicode_support)."""
    if ARABIC_FONT_PATH.exists():
        try:
            # fpdf2 >= 2.5 deprecates the `uni` kwarg; both work for now.
            pdf.add_font("Amiri", "", str(ARABIC_FONT_PATH))
            return "Amiri", True
        except Exception:
            pass
    return "Helvetica", False


def generate_pdf(mcqs: list[dict], title: str = "MCQ Quiz") -> bytes:
    """Render an MCQ list to a PDF and return the bytes."""
    any_arabic = any(_has_arabic(str(q.get(k, ""))) for q in mcqs
                     for k in ("q", "a", "b", "c", "d"))

    # Probe font before constructing the doc so the PDF object knows
    # which family to use as its default.
    probe = FPDF()
    family, has_unicode = _add_unicode_font(probe)
    del probe

    pdf = _PDF(font_family=family, has_rtl=any_arabic and has_unicode)
    if has_unicode:
        pdf.add_font("Amiri", "", str(ARABIC_FONT_PATH))
    pdf.add_page()

    # Title
    pdf.set_font(family, "", 18)
    title_render = _shape(title) if (any_arabic and has_unicode) else title
    pdf.cell(0, 12, title_render, align="C")
    pdf.ln(14)

    # Warning banner if Arabic content but no Arabic font installed
    if any_arabic and not has_unicode:
        pdf.set_font(family, "", 9)
        pdf.set_text_color(180, 60, 60)
        warn = ("Arabic font not found at fonts/Amiri-Regular.ttf — "
                "Arabic text may not render correctly. "
                "See README for setup.")
        pdf.multi_cell(0, 6, warn)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

    # Questions
    align = "R" if (any_arabic and has_unicode) else "L"
    for i, q in enumerate(mcqs, 1):
        pdf.set_font(family, "", 12)
        q_text = f"Q{i}. {q.get('q','')}"
        pdf.multi_cell(0, 7, _shape(q_text) if (any_arabic and has_unicode) else q_text,
                       align=align)
        pdf.ln(1)

        pdf.set_font(family, "", 11)
        for letter in ("a", "b", "c", "d"):
            opt = f"  {letter.upper()}) {q.get(letter,'')}"
            pdf.multi_cell(0, 6, _shape(opt) if (any_arabic and has_unicode) else opt,
                           align=align)
        pdf.ln(4)

    # Answer key on a new page
    pdf.add_page()
    pdf.set_font(family, "", 16)
    ans_title = "Answer Key" if not (any_arabic and has_unicode) else _shape("الإجابات")
    pdf.cell(0, 10, ans_title, align="C")
    pdf.ln(12)

    pdf.set_font(family, "", 11)
    cols = 4
    per_col = (len(mcqs) + cols - 1) // cols
    for col in range(cols):
        x = 10 + col * 47
        y = pdf.get_y() if col == 0 else pdf.get_y()  # reset on each column
        pdf.set_xy(x, 35 if pdf.page_no() > 1 else 35)
        start = col * per_col
        end = min(start + per_col, len(mcqs))
        for idx in range(start, end):
            ans = str(mcqs[idx].get("answer", "?")).upper()
            line = f"Q{idx+1}: {ans}"
            pdf.set_xy(x, pdf.get_y())
            pdf.cell(40, 6, line)
            pdf.ln(6)

    out = pdf.output(dest="S")
    # fpdf2 returns bytearray; normalize to bytes
    return bytes(out) if isinstance(out, (bytes, bytearray)) else out.encode("latin-1")


# ─────────────────────────────────────────────────────────────
# Anki export
# ─────────────────────────────────────────────────────────────
_ANKI_MODEL_ID = 1607392319   # any random-ish positive int, stable across runs
_ANKI_CSS = """
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 18px;
  text-align: start;
  color: #222;
  background: #fafafa;
  padding: 16px;
}
.question { font-weight: 600; margin-bottom: 12px; }
.opt { margin: 4px 0; }
hr#answer { margin: 16px 0; }
.answer { font-weight: 700; color: #1976d2; }
"""

_ANKI_MODEL = genanki.Model(
    _ANKI_MODEL_ID,
    "MCQ Quiz (4 options)",
    fields=[
        {"name": "Question"},
        {"name": "OptionA"},
        {"name": "OptionB"},
        {"name": "OptionC"},
        {"name": "OptionD"},
        {"name": "Answer"},
    ],
    templates=[{
        "name": "MCQ Card",
        "qfmt": (
            '<div class="card" dir="auto">'
            '<div class="question">{{Question}}</div>'
            '<div class="opt">A) {{OptionA}}</div>'
            '<div class="opt">B) {{OptionB}}</div>'
            '<div class="opt">C) {{OptionC}}</div>'
            '<div class="opt">D) {{OptionD}}</div>'
            '</div>'
        ),
        "afmt": (
            '{{FrontSide}}<hr id="answer">'
            '<div class="card answer" dir="auto">'
            '✓ {{Answer}}'
            '</div>'
        ),
    }],
    css=_ANKI_CSS,
)


def generate_anki(mcqs: list[dict], deck_name: str = "MCQ Deck") -> bytes:
    """Build an Anki .apkg in a temp file and return its bytes."""
    deck_id = random.randrange(1 << 30, 1 << 31)
    deck = genanki.Deck(deck_id, deck_name)

    for q in mcqs:
        ans_letter = str(q.get("answer", "")).upper()
        ans_text_map = {
            "A": q.get("a", ""),
            "B": q.get("b", ""),
            "C": q.get("c", ""),
            "D": q.get("d", ""),
        }
        ans_text = ans_text_map.get(ans_letter, "")
        answer_field = f"{ans_letter}) {ans_text}" if ans_text else ans_letter

        note = genanki.Note(
            model=_ANKI_MODEL,
            fields=[
                str(q.get("q", "")),
                str(q.get("a", "")),
                str(q.get("b", "")),
                str(q.get("c", "")),
                str(q.get("d", "")),
                answer_field,
            ],
        )
        deck.add_note(note)

    pkg = genanki.Package(deck)
    # genanki only writes to a file path — use a temp file then read it back.
    with tempfile.NamedTemporaryFile(suffix=".apkg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        pkg.write_to_file(tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
