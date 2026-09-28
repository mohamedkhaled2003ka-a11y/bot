# MedQuiz Ai — v6 integration guide

Five drop-in modules + one font. **Your `bot.py` is edited surgically — not rewritten.**
Everything below is written against the exact `bot.py` you uploaded (2546 lines), so
the "find this / replace with" blocks match your real code.

> I never had your `db.py`, `export.py`, or `migrate.py`, so I could not run `bot.py`
> end-to-end here. **Test on a staging bot before production.** Each module was tested
> in isolation (Arabic PDF rendering, PDF conversions, reciter-URL probing, PDF image
> extraction) — see "What was actually tested" at the bottom.

---

## 0. Files in this drop

| File | Purpose | Request # |
|---|---|---|
| `islamic.py` | **Replaces** your current `islamic.py`. Fixes reciters, adds download + Azkar categories + daily reminders | 1, 11, 12 |
| `qexport.py` | Arabic-correct MCQ → PDF export engine | 2 |
| `pdftools.py` | Conversion engine (Word/PPT/Image/PDF, merge/split/compress) | 3 |
| `pdftools_menu.py` | Telegram UI for the PDF Tools menu | 3 |
| `medical.py` | Upload-and-analyze medical file/image/PDF (fixes reversed Spot Diagnosis) | 5, 6 |
| `fonts/Amiri-Regular.ttf` | Arabic font the PDF export embeds | 2 |

Put all five `.py` next to `bot.py`, and the `fonts/` folder next to them too:

```
bot.py  addons.py  islamic.py  qexport.py  pdftools.py  pdftools_menu.py  medical.py
db.py  export.py  migrate.py  dbtools.py
fonts/Amiri-Regular.ttf
```

---

## 1. requirements.txt — add these

```
arabic-reshaper      # Arabic letter shaping for PDF export
python-bidi          # right-to-left ordering for PDF export
reportlab            # PDF export engine (you may already have it)
pikepdf              # PDF compress
pdf2docx             # PDF -> Word
python-pptx          # PPT/PDF slide handling
Pillow               # image handling (usually already present)
```

`PyMuPDF` (`fitz`) you already use. For **Word→PDF / PPT→PDF / PDF→PPT** the host
also needs **LibreOffice** installed (`soffice` on PATH). The pure-Python tools
(Image↔PDF, merge, split, compress, PDF→Word) work without it; the menu hides the
LibreOffice-only buttons automatically when `soffice` is absent.

```bash
# Debian/Ubuntu host:
apt-get install -y libreoffice
```

---

## 2. Wire the modules into `main()`

In `bot.py`, your `main()` already does `addons.register(app)` and
`islamic.register(app)` near line 2524. Add the two new modules right after.

**Find:**
```python
    addons.register(app)
    islamic.register(app)
```

**Replace with:**
```python
    import medical, pdftools_menu
    addons.register(app)
    islamic.register(app)        # uses the NEW islamic.py (just overwrite the file)
    medical.register(app)        # group -2: runs before addons' interceptors
    pdftools_menu.register(app)
```

Nothing else in `main()` changes. `concurrent_updates(True)` is already there.

---

## 3. Arabic PDF export (request #2)

Your `on_export()` (~line 1597) currently calls `exporter.generate_pdf`, which is
where Arabic breaks. Swap **only the PDF branch** to the new engine.

**Find:**
```python
        if fmt == "pdf":
            data = await asyncio.to_thread(exporter.generate_pdf, mcqs, "MCQ Quiz")
            filename = "quiz.pdf"
```

**Replace with:**
```python
        if fmt == "pdf":
            import qexport
            data = await asyncio.to_thread(
                qexport.build_quiz_pdf, mcqs,
                title="أسئلة الاختيار من متعدد", with_answers=True)
            filename = "quiz.pdf"
```

The Anki branch is untouched. `build_quiz_pdf` returns `bytes`, shapes Arabic
correctly (RTL + letter joining), leaves English/numbers alone, and marks the
correct answer in green with the Arabic label `(الإجابة الصحيحة)`.

> The font is found automatically if `fonts/Amiri-Regular.ttf` sits next to the
> modules. To use a different path: `build_quiz_pdf(mcqs, font_path="/abs/path.ttf")`.

---

## 4. PDF Tools menu (request #3)

`pdftools_menu.register(app)` (step 2) already adds the `/pdftools` command and its
whole inline flow. To surface it as a reply-keyboard button:

**a) Add labels** near your other `BTN_*` constants (~line 183):
```python
BTN_PDFTOOLS = "🛠️ أدوات PDF"
BTN_MEDICAL  = "🩺 تشخيص طبي"
```

**b) Add buttons** to the keyboards (`build_user_reply_keyboard`, ~line 540, and the
two admin keyboards). For the user keyboard, e.g.:
```python
        [KeyboardButton(BTN_PDFTOOLS), KeyboardButton(BTN_MEDICAL)],
```

**c) Route them** in `handle_text`, next to the `BTN_SPOT` block (~line 1885):
```python
    if text == BTN_PDFTOOLS:
        import pdftools_menu
        await pdftools_menu.cmd_pdftools(update, context)
        return
    if text == BTN_MEDICAL:
        import medical
        await medical.cmd_diagnose(update, context)
        return
```

Supported ops: Image→PDF, PDF→Images, Merge, Split, Compress, PDF→Word (all
pure-Python); Word→PDF, PPT→PDF, PDF→PPT (need LibreOffice). PDF→PPT renders each
page as a full-slide image — LibreOffice cannot produce *editable* slides from a
PDF, so image-per-slide is the reliable approach.

---

## 5. Medical analysis / Spot Diagnosis fix (requests #5, #6)

The old Spot Diagnosis hid the answer in a spoiler ("guess what this is") — that's
the reversed behaviour you flagged. `medical.py` replaces that intent: the user
uploads a PDF/image/lab/scan and the bot **analyzes it openly** and returns
modality, findings, impression, differential, and a recommendation, plus an Arabic
study-aid disclaimer.

- Entry point: `/diagnose` (or the `BTN_MEDICAL` button from step 4).
- It registers document/photo interceptors at **group -2**, so when the user is in
  medical mode it grabs the file before `addons`' Spot/Qfile interceptors (-1) see it.
- For PDFs it extracts text **and** embedded images, and falls back to rasterizing
  scanned pages, then sends everything to `bot.call_gemini` with a structured prompt.

You can leave your existing `/spot` in place or drop the Spot button — they no longer
collide. If you want the Spot button to point at the new analyzer, just route
`BTN_SPOT` to `medical.cmd_diagnose` instead of `addons.cmd_spot`.

---

## 6. Reciters / Quran / Azkar (requests #1, #11, #12)

Just overwrite `islamic.py` with the new one. Why only Alafasy worked before: the old
code hard-coded a `/128/` bitrate and a few edition IDs whose CDN folders either don't
match or only publish 64 kbps audio, so they silently failed.

The new module **probes each reciter against the CDN at startup/first use** (trying
128 then 64 kbps via a tiny range request), keeps only the ones that actually resolve,
remembers the working bitrate per reciter, and caches the result via
`db.set_state("quran_reciters_verified", ...)`. So the menu self-corrects on *your*
server regardless of my guesses — 17 canonical reciters are listed, and whichever your
host can reach are the ones shown.

Added: **download-as-file** button for any surah, an Azkar **prayer** category
alongside morning/evening/sleep, and **opt-in daily reminders** (morning ~6am /
evening ~6pm server time) that re-arm after restart through `post_init`.

`handle_search_text` is preserved, so your existing hook near the top of
`handle_text` (`if await islamic.handle_search_text(...)`) keeps working unchanged.

> Needs network egress to `api.alquran.cloud` and `cdn.islamic.network`. The
> reminders use PTB's job-queue — make sure `python-telegram-bot[job-queue]` is
> installed (`pip install "python-telegram-bot[job-queue]"`).

---

## 7. Database safety (request #10) — nothing to do

No schema changes in this drop. The new modules only read/write via the `db` API
your `bot.py` already uses (plus `db.get_state`/`db.set_state`, which you already use
for global flags). Your `dbtools.py predeploy` flow (backup → verify → additive
migrate → verify) is unchanged and remains the right pre-deploy step. **No user rows
are touched.**

---

## 8. /start, main menu sections, admin panel (requests #4, #7, #8) — partial

Your `/start` handler and reply keyboards already exist and already render a menu.
Steps 3–4 above add the PDF Tools and Medical entries to that menu, which covers the
new *functional* sections (Quran, Azkar, PDF Tools, AI Chat, Medical, Settings, Admin
all now have an entry point).

What I deliberately did **not** auto-rewrite, because it means editing your
2546-line `bot.py` and the `db.py` I don't have:

- A full visual redesign of the menu into labelled "section" sub-menus.
- Expanding the admin panel with *new* controls (model selection, reciter management
  UI). The existing admin panel — enable/disable bot, add/list users, analytics,
  broadcast, backup/restore, smart-mode dashboard — already covers most of #8;
  "manage reciters" is now effectively automatic (self-verifying), and "control AI
  models" already exists via your `GEMINI_MODEL` config.

If you want the cosmetic section-menu redesign, tell me and I'll write it as another
surgical patch against your `start()` / keyboard builders.

**Image Generation (part of #7) is not implemented** — it's a brand-new capability
(needs an image model + billing decisions) rather than a fix, so I left it out rather
than ship something untested.

---

## What was actually tested (in isolation, not against your live bot)

- **qexport**: rendered a sample Arabic/English/number MCQ PDF, converted to PNG,
  visually verified RTL joining and the green correct-answer label. (`sample_quiz_ar_PREVIEW.pdf` is included.)
- **pdftools**: Image→PDF, PDF→Images, merge, split, compress, PDF→Word all pass;
  Word→PDF, PPT→PDF, PDF→PPT pass with LibreOffice present.
- **medical**: PDF text+image extraction and scanned-page rasterization pass; the
  Gemini call mirrors your `addons` pattern (not executed here — no key/db).
- **islamic**: reciter-verification + bitrate fallback + db caching verified with a
  mocked HTTP layer (e.g. a reciter that only has 64 kbps is kept at 64; an
  unreachable ID is dropped; the cached result reloads on restart).
- **pdftools_menu**: op-dispatch (img2pdf/split/compress) passes; the Telegram flow
  itself needs your bot/db to exercise — verify on staging.

All five modules `py_compile` clean.
