# MCQ Bot v5 — final files

## Files in this drop

- **bot.py**      — your bot, fully updated (replaces your current bot.py).
- **addons.py**   — NEW. Backup/restore, batched generation, smart-mode
                    dashboard, quiz-from-file, spot diagnosis, /health, /tasks.
- **islamic.py**  — NEW. Azkar, Quran reading, audio recitations, search,
                    bookmarks.

## Files you keep UNCHANGED (I never had them)

- db.py, export.py, migrate.py — leave exactly as they are. v5 only calls the
  same `db` functions your old bot.py already used, plus `db.get_state` /
  `db.set_state` (which you already use for global flags) for Quran bookmarks.

## requirements.txt — add these two lines

```
python-docx
httpx
```

(`httpx` is already pulled in by python-telegram-bot; listed for safety.
`python-docx` is only needed for the "quiz from DOCX file" feature.)

## Deploy steps

1. Drop `bot.py`, `addons.py`, `islamic.py` next to `db.py` / `export.py` /
   `migrate.py`.
2. `pip install -r requirements.txt` (after adding the two lines above).
3. **Persistence (the real fix for "users lost on redeploy"):** make sure your
   SQLite DB lives on a persistent volume and point `BOT_DATA_DIR` at it. The
   `/backup` + `/restore` buttons are your safety net, not a substitute.
4. Make sure the host can reach `api.alquran.cloud` and `cdn.islamic.network`
   for the Islamic section.
5. Run on a **staging bot first** — I can't run this against your real
   db.py/Telegram, so verify there before pointing production at it.

## What each feature maps to

| Request | Where |
|---|---|
| 1. Backup/restore | `/backup`, `/restore` (admin) + persistent-volume note |
| 2. Concurrency | `concurrent_updates(True)` + tracked background tasks |
| 3. Large sets 100/200+ | `addons.generate_mcqs_smart` batching |
| 4. Smart mode default-off + dashboard + bulk | `set_setting(...,False)` on add + `/smartdash` |
| 5. Quiz from question file | `/qfile` button (PDF/DOCX/TXT) |
| 6. Spot Diagnosis | `/spot` button (image → spoiler answer) |
| 7. Islamic section | `/islamic` button |
| 8. Performance/stability | concurrency, threads, tracked tasks, `/health` |
| 9. Admin tools | existing panel + `/smartdash` `/backup` `/restore` `/health` `/tasks` |
```
