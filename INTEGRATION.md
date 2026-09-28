# Integration guide — MCQ bot v5

Two new files (`addons.py`, `islamic.py`) plus a handful of small edits to
your existing `bot.py`. Nothing here rewrites your working handlers.

---

## 0. Dependencies

Add to `requirements.txt`:

```
python-docx        # quiz-from-DOCX
httpx              # Quran API (PTB already pulls this in, listed for safety)
```

---

## 1. Persistence — the real fix for "users lost on redeploy"  (#1)

The backup/restore buttons are a **safety net**, not the cure. Users vanish
on redeploy because your SQLite file lives in ephemeral storage that the
platform wipes. Fix the storage location first:

- Point the DB at a **persistent volume / disk** your platform keeps across
  deploys, and set `BOT_DATA_DIR` (your code already reads it) to that path,
  e.g. `BOT_DATA_DIR=/data`.
- On Railway/Render/Fly add a mounted volume; on a VPS just use an absolute
  path outside the deploy dir; on Docker use a named volume.

Once the DB is on a persistent disk, redeploys won't lose anyone. Use
`/backup` regularly anyway and keep the JSON somewhere safe.

`/backup`  → admin gets a JSON file of all users.
`/restore` → admin sends that JSON back; membership, tier, settings and bonus
are restored.

---

## 2. Concurrency — stop one big PDF blocking everyone  (#2)

PTB processes updates **sequentially by default**. One line fixes it. In
`main()` change the builder:

```python
app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .concurrent_updates(True)        # <-- ADD THIS
    .post_init(post_init)
    .post_shutdown(post_shutdown)
    .build()
)
```

Your CPU-bound PDF parsing already runs in `asyncio.to_thread`, so with
concurrent updates on, users no longer wait in line.

---

## 3. Large question sets (100/200+)  (#3)

Big sets fail because one Gemini call hits `MAX_OUTPUT_TOKENS` (8192) and the
JSON truncates. Replace the generation block inside `generate_mcqs_and_send`:

**Find this:**

```python
    try:
        if pdf_images or (text and not image_data):
            mcqs = await generate_mcqs_from_pdf(text, pdf_images, n_questions,
                                                difficulty, language)
        elif image_data:
            mcqs = await generate_mcqs_from_image(image_data, image_mime, n_questions,
                                                  difficulty, language)
        else:
            mcqs = await generate_mcqs_from_text(text, n_questions, difficulty, language)
    except Exception as e:
```

**Replace with:**

```python
    try:
        mcqs = await addons.generate_mcqs_smart(
            text=text, pdf_images=pdf_images,
            image_data=image_data, image_mime=image_mime,
            n=n_questions, difficulty=difficulty, language=language)
    except Exception as e:
```

It batches in chunks of 25, runs up to 4 in parallel, merges and de-dupes.
There is no "abuse at >80" logic in your code — the only cap is
`MAX_QUESTIONS = 200`. Raise it if you want more.

---

## 4. Smart Mode default OFF for new users  (#4)

Smart-mode admin toggles already exist. To make it **off by default**, add
one line right after each place you allow a user. There are three:

- in `start()` referral branch, after `await db.allow_user(user_id)`
- in `handle_text()` single-add branch, after `await db.allow_user(new_id)`
- in `process_multi_add()`, after `await db.allow_user(uid)`

Add:

```python
await db.set_setting(<that id>, "smart_chat_enabled", False)
```

Dashboard + bulk: `/smartdash` (admin) shows counts, lists who has it on,
per-user toggle, and "enable/disable for all" buttons.

> Cleaner alternative: set the column default to `False` in `db.py`'s User
> model so you don't touch call sites. Either works.

---

## 5–6. Quiz-from-file & Spot Diagnosis  (#5, #6)

No bot.py edits needed — `addons.register(app)` adds:
- `/qfile` → user sends PDF/DOCX/TXT of existing questions → quiz.
- `/spot`  → user sends image(s) → identification in spoiler format.

Both intercept documents/photos only while their mode is active (handler
group -1 + `ApplicationHandlerStop`), so your normal MCQ flow is untouched.

---

## 7. Islamic section  (#7)

`islamic.register(app)` adds `/islamic` (Azkar, Quran read, audio, search,
bookmarks). One extra edit so search-by-text works: near the top of
`handle_text()` (right after `text = ...`), add:

```python
    import islamic
    if await islamic.handle_search_text(update, context, text):
        return
```

Network: your host must reach `api.alquran.cloud` and `cdn.islamic.network`.
Azkar are a curated selection — extend the `AZKAR` dict in `islamic.py` freely.

---

## 8–9. Stability / admin tools

- `/health` — uptime, memory, active keys, user count.
- `/tasks`  — live background jobs.
- Long jobs run via `addons.track(...)` so exceptions are logged, not fatal.

---

## Wiring summary — add to `main()` after `.build()`

```python
import addons, islamic
...
addons.register(app)
islamic.register(app)
```

(Plus the `concurrent_updates(True)` line and the `generate_mcqs_smart` swap.)

Optionally surface the new commands as reply-keyboard buttons by adding the
labels to your keyboard builders and routing them in `handle_text` to the
matching `cmd_*` — the commands work as-is without that.

---

## Honest caveats

- I don't have your `db.py` / `export.py` / `migrate.py`, so this is written
  against the `db` API your `bot.py` already calls and against PTB/Gemini as
  used there. **Test on a staging bot before production.**
- Restore's bonus is additive — don't restore the same file twice.
- Quran text/audio is fetched live from public APIs; treat them as a
  best-effort dependency and keep the try/except logging in place.
