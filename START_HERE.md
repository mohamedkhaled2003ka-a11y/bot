# START HERE — MCQ bot v5

## 1. File inventory

### Files I created/updated (in this package)
| File | What it is |
|---|---|
| `bot.py` | Your bot, fully updated. **Replaces** your current `bot.py`. |
| `addons.py` | NEW. Backup/restore, batched generation, smart-mode dashboard, quiz-from-file, spot diagnosis, `/health`, `/tasks`. |
| `islamic.py` | NEW. Azkar, Quran reading, audio recitations, search, bookmarks. |
| `dbtools.py` | NEW. Standalone, safe backup + additive migration tool (stdlib only). |
| `requirements.txt` | Dependencies (pin to your existing versions). |
| `RUNBOOK_safe_deploy.md` | The safe deploy / migration procedure. |
| `README_v5.md`, `INTEGRATION.md` | Feature notes + how the wiring works. |

### Files you ALREADY HAVE and must keep (I never had them)
`db.py`, `export.py`, `migrate.py` — leave them exactly as they are. The new
code only calls the same `db` functions your old `bot.py` already used.

> So the full bot folder is:
> `bot.py  addons.py  islamic.py  dbtools.py  db.py  export.py  migrate.py  requirements.txt`

---

## 2. Install

```bash
# Python 3.10+ recommended
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

---

## 3. Configure (environment variables)

Required:
```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."        # from @BotFather
export GEMINI_API_KEY="key1,key2,key3"           # one or many (comma/space/newline separated)
# (GEMINI_API_KEYS works too; up to 20 keys are rotated automatically)
```

Recommended / optional:
```bash
export BOT_USERNAME="YourBot"        # without @ — enables referral links
export GEMINI_MODEL="gemini-2.5-flash"
export REFERRAL_REWARD="3"
export BOT_DATA_DIR="/data"          # PERSISTENT folder for the SQLite DB (see step 5)
```

Set the two admin Telegram IDs in `bot.py` (`ADMIN_IDS = { ... }`) and
`ADMIN_USERNAME` to your handle.

---

## 4. Safe pre-deploy (do this BEFORE first run of new code, bot stopped)

```bash
export DB_PATH="$BOT_DATA_DIR/bot.db"      # or wherever your db lives
python dbtools.py discover                 # confirm it finds your DB + users table
python dbtools.py predeploy                # backup -> verify -> additive migrate -> verify
```

(For v5 there's no schema change, so `predeploy` mostly just makes a verified
backup — which is exactly what you want before any deploy.)

---

## 5. Persistence — the real fix for "users lost on redeploy"

Make sure the SQLite file lives on a **persistent volume** that survives
redeploys, and point `BOT_DATA_DIR` (and `DB_PATH` for dbtools) at it. On
Railway/Render/Fly add a mounted volume; on a VPS use an absolute path outside
the deploy directory; in Docker use a named volume. Keep the `backups/` folder
on that volume too, and schedule `python dbtools.py backup` (e.g. cron).

---

## 6. Run

**Polling (simplest — local, VPS, most hosts):**
```bash
python bot.py
```
That's it. The bot logs `Bot starting in POLLING mode …`.

**Webhook (if you run a public HTTPS endpoint):**
```bash
export WEBHOOK_URL="https://your.domain"     # presence of this switches to webhook mode
export WEBHOOK_PORT="8443"
export WEBHOOK_PATH="telegram"
export WEBHOOK_SECRET="some-random-string"   # optional
python bot.py
```

To keep it alive in production, run under a process manager:
```bash
# systemd, pm2, supervisor, or quick-and-dirty:
nohup python bot.py > bot.log 2>&1 &
```

---

## 7. Quick smoke test in Telegram

1. Send `/start` — you should get the keyboard (admin or user layout).
2. As admin: `/health` and `/tasks` respond; `/backup` sends you a JSON file.
3. Send a PDF → pick language/difficulty → ask for e.g. 120 questions
   (batched generation handles large sets).
4. `/spot` then send an image → identification with a tap-to-reveal answer.
5. `/qfile` then send a PDF/DOCX/TXT of questions → quiz.
6. `/islamic` → Azkar / Quran (needs network to `api.alquran.cloud` and
   `cdn.islamic.network`).
7. `/smartdash` (admin) → smart-mode dashboard + bulk toggle.

---

## Honest reminders

- I can't run `bot.py` end-to-end here (it needs your `db.py`/`export.py`/
  `migrate.py` and live Telegram/Gemini), so **test on a staging bot first**.
  `dbtools.py` is fully tested and safe to run on your real DB.
- If your users table isn't named `users`, edit the table name in the
  `EXPECTED_COLUMNS` block at the top of `dbtools.py` before `predeploy`.
- Pin the dependency versions in `requirements.txt` to whatever you already
  run, so the deploy doesn't pull in surprise upgrades.
