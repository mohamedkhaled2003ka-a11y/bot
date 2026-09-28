# Safe deploy / migration runbook

`dbtools.py` is standalone (Python stdlib only). It never imports the bot,
`db.py`, or SQLAlchemy, and it has **no code path that can DROP or DELETE your
data**. Every migration is additive and atomic.

## One-time setup

1. Put `dbtools.py` next to your bot files.
2. Tell it where the DB is (first match wins):
   - `--db /path/to/bot.db`, or
   - `export DB_PATH=/path/to/bot.db`, or
   - it parses `DATABASE_URL` (e.g. `sqlite+aiosqlite:///data/bot.db`), or
   - it scans `BOT_DATA_DIR` for a single `*.db`/`*.sqlite` file.
3. Optionally `export BOT_BACKUP_DIR=/persistent/backups` (defaults to `./backups`).
4. Confirm it sees the right database and your `users` table:
   ```
   python dbtools.py discover
   ```
   If your users table isn't named `users`, edit the table name in the
   `EXPECTED_COLUMNS` block at the top of `dbtools.py`.

## Every deploy / update — run this BEFORE starting the new code

**Stop the bot, then:**

```
python dbtools.py predeploy
```

That does, in order, aborting safely if any step fails:

1. **Backup** — consistent `.db` snapshot (online backup API, safe even under
   WAL) + a full JSON dump + a manifest with sha256 checksums and per-table
   row counts.
2. **Verify** the live DB (`PRAGMA integrity_check` + `foreign_key_check`).
3. **Migrate** — additive only, inside a single transaction. If anything
   fails it rolls back completely (SQLite DDL is transactional), so the DB is
   never left half-changed. Row counts are checked before/after; it aborts if
   any table would lose rows.
4. **Re-verify**.

Then start the new bot code.

> For bot **v5 specifically there is no schema change** — `predeploy` will
> report "no changes needed" apart from creating its own `_schema_migrations`
> bookkeeping table. You still get a verified backup, which is the point.

## If something goes wrong — restore

```
python dbtools.py restore --from backups/bot_YYYYMMDD_HHMMSS.db
```

It verifies the snapshot, saves a `*_pre-restore_*.db` copy of whatever is
currently live, then atomically replaces the live file. (Stop the bot first.)

## Disaster recovery from the JSON dump (e.g. brand-new empty volume)

```
python dbtools.py import-json --in backups/bot_YYYYMMDD_HHMMSS.json --target /data/bot.db
```

Defaults to insert-or-ignore (never overwrites existing rows). Add `--upsert`
to overwrite rows with the same primary key.

## The real root-cause for "users lost on redeploy"

This tool is your safety net, but the cure is **storage**: keep the SQLite
file on a persistent volume that survives redeploys, and point `BOT_DATA_DIR`
/ `DB_PATH` at it. If the file lives in the ephemeral deploy directory, it
will keep getting wiped no matter how good the backups are — so schedule
`python dbtools.py backup` (e.g. via cron) and keep the backup directory on
the persistent volume too.

## Future schema changes

When a future version genuinely needs a new column/table/index, add it to the
`EXPECTED_COLUMNS` / `EXPECTED_TABLES` / `EXPECTED_INDEXES` blocks at the top
of `dbtools.py`, run `python dbtools.py migrate` (dry run) to review, then
`predeploy`. Anything destructive (DROP/DELETE/RENAME/TRUNCATE) is rejected by
design — those need a deliberate, separately-reviewed script.

## Tested behaviours (verified, not theoretical)

- Up-to-date DB → migrate is a no-op (only adds bookkeeping table).
- Older DB missing columns → columns added with defaults, **all rows kept**,
  integrity ok, applied atomically.
- Full JSON export → rebuild into a fresh empty DB → identical row counts.
- Restore into a corrupt live file and into a missing path → both succeed,
  with a pre-restore safety copy.
- A DROP statement injected into the plan → refused before any change.
