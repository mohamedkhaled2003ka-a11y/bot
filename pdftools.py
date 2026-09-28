"""
pdftools.py — PDF conversion/utility toolbox for the bot (#3)
=============================================================
All functions take/return raw `bytes` so they plug straight into Telegram
upload/download. Pure-Python tools work on any host. The Office conversions
(Word/PowerPoint <-> PDF) require LibreOffice (`soffice`) on the host; they
detect it and raise a clear error if missing.

Pure-Python (always available):
    images_to_pdf(list_of_image_bytes) -> pdf_bytes
    pdf_to_images(pdf_bytes, dpi=150)  -> list[png_bytes]
    merge_pdfs(list_of_pdf_bytes)      -> pdf_bytes
    split_pdf(pdf_bytes)               -> list[(filename, pdf_bytes)]
    compress_pdf(pdf_bytes)            -> pdf_bytes
    pdf_to_word(pdf_bytes)             -> docx_bytes        (pdf2docx)

Needs LibreOffice on host:
    office_to_pdf(file_bytes, ext)     -> pdf_bytes         (docx/pptx -> pdf)
    pdf_to_office(pdf_bytes, target)   -> file_bytes        (pdf -> docx/pptx)

`soffice_available()` lets the bot show/hide those buttons accordingly.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple

import fitz  # PyMuPDF
import pikepdf
from PIL import Image


# ---------------------------------------------------------------- detection
def soffice_available() -> bool:
    return shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def _soffice_bin() -> str:
    return shutil.which("soffice") or shutil.which("libreoffice")


# ---------------------------------------------------------------- pure python
def images_to_pdf(images: List[bytes]) -> bytes:
    """Combine images (jpg/png/webp...) into a single PDF, one image per page."""
    if not images:
        raise ValueError("no images provided")
    pil_pages = []
    for b in images:
        im = Image.open(io.BytesIO(b))
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        pil_pages.append(im)
    out = io.BytesIO()
    pil_pages[0].save(out, format="PDF", save_all=True,
                      append_images=pil_pages[1:])
    return out.getvalue()


def pdf_to_images(pdf_bytes: bytes, dpi: int = 150) -> List[bytes]:
    """Rasterize each PDF page to a PNG."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        pages.append(pix.tobytes("png"))
    doc.close()
    return pages


def merge_pdfs(pdfs: List[bytes]) -> bytes:
    """Merge multiple PDFs into one, in the given order."""
    if not pdfs:
        raise ValueError("no PDFs provided")
    out = pikepdf.Pdf.new()
    for b in pdfs:
        src = pikepdf.Pdf.open(io.BytesIO(b))
        out.pages.extend(src.pages)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def split_pdf(pdf_bytes: bytes) -> List[Tuple[str, bytes]]:
    """Split into one PDF per page. Returns [(filename, bytes), ...]."""
    src = pikepdf.Pdf.open(io.BytesIO(pdf_bytes))
    out = []
    n = len(src.pages)
    for i in range(n):
        single = pikepdf.Pdf.new()
        single.pages.append(src.pages[i])
        buf = io.BytesIO()
        single.save(buf)
        out.append((f"page_{i+1:03d}.pdf", buf.getvalue()))
    return out


def compress_pdf(pdf_bytes: bytes) -> bytes:
    """Lossless-ish shrink: stream compression + object stream rebuild +
    downsample large embedded images. Returns the smaller of original/result."""
    # 1) image downsampling via PyMuPDF
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.rewrite_images(dpi_threshold=200, dpi_target=150, quality=70)
        tmp = doc.tobytes(garbage=4, deflate=True, clean=True)
        doc.close()
    except Exception:
        tmp = pdf_bytes
    # 2) structural compression via pikepdf
    try:
        pdf = pikepdf.Pdf.open(io.BytesIO(tmp))
        buf = io.BytesIO()
        pdf.save(buf, compress_streams=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
        result = buf.getvalue()
    except Exception:
        result = tmp
    return result if len(result) < len(pdf_bytes) else pdf_bytes


def pdf_to_word(pdf_bytes: bytes) -> bytes:
    """Convert PDF -> DOCX using pdf2docx (pure python, layout-preserving)."""
    from pdf2docx import Converter
    with tempfile.TemporaryDirectory() as td:
        pin = os.path.join(td, "in.pdf")
        pout = os.path.join(td, "out.docx")
        with open(pin, "wb") as f:
            f.write(pdf_bytes)
        cv = Converter(pin)
        try:
            cv.convert(pout)
        finally:
            cv.close()
        with open(pout, "rb") as f:
            return f.read()


# ---------------------------------------------------------------- libreoffice
def _libreoffice_convert(src_bytes: bytes, src_ext: str, target_ext: str) -> bytes:
    if not soffice_available():
        raise RuntimeError(
            "LibreOffice (soffice) is not installed on this host, so "
            "Word/PowerPoint conversions are unavailable. Install it with "
            "`apt-get install libreoffice` (or use a host that has it).")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, f"input.{src_ext}")
        with open(src, "wb") as f:
            f.write(src_bytes)
        # headless conversion; --convert-to writes <name>.<target_ext> in outdir
        proc = subprocess.run(
            [_soffice_bin(), "--headless", "--norestore", "--convert-to",
             target_ext, "--outdir", td, src],
            capture_output=True, timeout=180,
        )
        produced = os.path.join(td, f"input.{target_ext}")
        if not os.path.exists(produced):
            raise RuntimeError(
                "LibreOffice conversion failed: "
                + (proc.stderr.decode("utf-8", "ignore")[:300] or "no output file"))
        with open(produced, "rb") as f:
            return f.read()


def office_to_pdf(file_bytes: bytes, ext: str) -> bytes:
    """ext in {'docx','doc','pptx','ppt','xlsx','xls'} -> PDF bytes."""
    ext = ext.lower().lstrip(".")
    return _libreoffice_convert(file_bytes, ext, "pdf")


def pdf_to_pptx(pdf_bytes: bytes, dpi: int = 150) -> bytes:
    """PDF -> PPTX by placing each page as a full-slide image. This is the
    reliable approach (LibreOffice cannot import PDF into editable slides)."""
    from pptx import Presentation
    from pptx.util import Inches, Emu
    images = pdf_to_images(pdf_bytes, dpi=dpi)
    prs = Presentation()
    # match slide size to first page aspect ratio (default 10x7.5in)
    blank = prs.slide_layouts[6]
    sw, sh = prs.slide_width, prs.slide_height
    for png in images:
        slide = prs.slides.add_slide(blank)
        bio = io.BytesIO(png)
        from PIL import Image as _I
        iw, ih = _I.open(io.BytesIO(png)).size
        # fit image within slide, centered
        scale = min(sw / iw, sh / ih)
        w, h = int(iw * scale), int(ih * scale)
        left, top = int((sw - w) / 2), int((sh - h) / 2)
        slide.shapes.add_picture(bio, left, top, width=w, height=h)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def pdf_to_office(pdf_bytes: bytes, target: str) -> bytes:
    """target in {'docx','pptx'}. PDF->DOCX uses pdf2docx (best layout);
    PDF->PPTX renders each page as a full-slide image (reliable)."""
    target = target.lower().lstrip(".")
    if target == "docx":
        try:
            return pdf_to_word(pdf_bytes)
        except Exception:
            return _libreoffice_convert(pdf_bytes, "pdf", "docx")
    if target == "pptx":
        return pdf_to_pptx(pdf_bytes)
    raise ValueError("target must be 'docx' or 'pptx'")
