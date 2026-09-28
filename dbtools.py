#!/usr/bin/env python3
"""
dbtools.py — safe backup & migration for the MCQ bot's SQLite database
======================================================================

PURPOSE
-------
Run this BEFORE deploying / updating the bot. It guarantees that no existing
user, setting, subscription (tier), statistic, or any other row is ever lost:

  * Backups use SQLite's online backup API → a *consistent* snapshot even if
    the bot is running with WAL.
  * Migrations are ADDITIVE ONLY. They `ALTER TABLE ... ADD COLUMN`,
    `CREATE TABLE IF NOT EXISTS`, and `CREATE INDEX IF NOT EXISTS`. They NEVER
    drop, rename, recreate, or delete anything. The whole migration runs in a
    single transaction and rolls back atomically on any error (SQLite DDL is
    transactional), so the live DB is never left half-changed.
  * Every destructive verb is refused. There is no code path here that can
    DROP or DELETE your data.
  * Row counts are captured before & after; the tool aborts if any table's
    count would decrease.

It is dependency-free (Python stdlib only) and does NOT import the bot,
db.py, or SQLAlchemy — it works directly on the .db file, which is the safest
possible layer.

USAGE
-----
    python dbtools.py discover                 # find DB, show tables/columns/counts
    python dbtools.py backup                   # consistent .db snapshot + JSON dump
    python dbtools.py verify [path]            # integrity_check + counts
    python dbtools.py migrate                  # DRY RUN: show planned additive changes
    python dbtools.py migrate --apply          # backup first, then apply (atomic)
    python dbtools.py export-json --out FILE    # full DB -> JSON (all tables)
    python dbtools.py import-json --in FILE --target NEW.db   # rebuild into fresh DB
    python dbtools.py restore --from BACKUP.db  # restore a snapshot to the live DB
    python dbtools.py predeploy                # backup -> verify -> migrate --apply -> verify

DB PATH RESOLUTION (first match wins)
-------------------------------------
    1. --db PATH on the command line
    2. $DB_PATH
    3. sqlite path parsed from $DATABASE_URL  (e.g. sqlite+aiosqlite:///data/bot.db)
    4. a single *.db / *.sqlite / *.sqlite3 file under $BOT_DATA_DIR (or CWD)

If several candidate files are found you'll be asked to pass --db explicitly.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

# ----------------------------------------------------------------------
# DESIRED additive schema. Editable. The tool only ADDS what's missing.
#
# IMPORTANT: bot v5 needs NOTHING new here — your existing schema already has
# every field the bot uses. This block exists so that (a) if your DB predates
# some field, it is added non-destructively with a safe default, and (b) future
# changes have a single safe place to declare additions.
#
# Format:
#   EXPECTED_COLUMNS[table][column] = (sql_type, default_sql_or_None)
#   EXPECTED_TABLES[name]           = "CREATE TABLE IF NOT EXISTS ..."
#   EXPECTED_INDEXES[name]          = "CREATE INDEX IF NOT EXISTS ..."
#
# Columns are only added to a table that ALREADY EXISTS. We never create the
# core tables from scratch (db.py owns those); we only top up missing columns.
# ----------------------------------------------------------------------
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, str | None]]] = {
    # If your `users` table is older and missing any of these, they'll be
    # added with defaults that match how the bot reads them. Adjust the table
    # name if yours differs (see `discover` output).
    "users": {
        "tier":               ("TEXT",    "'free'"),
        "daily_limit":        ("INTEGER", "3"),
        "used_today":         ("INTEGER", "0"),
        "bonus_lectures":     ("INTEGER", "0"),
        "anonymous":          ("INTEGER", "0"),
        "protect_content":    ("INTEGER", "0"),
        "smart_chat_enabled": ("INTEGER", "0"),   # v5: OFF by default
        "total_generated":    ("INTEGER", "0"),
        "referrals_made":     ("INTEGER", "0"),
        "allowed":            ("INTEGER", "0"),
        "last_seen":          ("TEXT",    None),
        "first_name":         ("TEXT",    None),
        "last_name":          ("TEXT",    None),
        "username":           ("TEXT",    None),
    },
}

EXPECTED_TABLES: dict[str, str] = {
    # Bookkeeping table this tool maintains. Safe to create.
    "_schema_migrations": (
        "CREATE TABLE IF NOT EXISTS _schema_migrations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " step TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    ),
}

EXPECTED_INDEXES: dict[str, str] = {
    # Example (commented): speed up allowed-user lookups if your schema allows.
    # "idx_users_allowed": "CREATE INDEX IF NOT EXISTS idx_users_allowed ON users(allowed)",
}

# Verbs we refuse to ever emit.
_FORBIDDEN = re.compile(r"\b(DROP|DELETE|TRUNCATE|RENAME)\b", re.IGNORECASE)


# ======================================================================
# Helpers
# ======================================================================
def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def resolve_db_path(cli_db: str | None) -> Path:
    # 1. explicit
    if cli_db:
        return Path(cli_db).expanduser().resolve()
    # 2. DB_PATH
    if os.environ.get("DB_PATH"):
        return Path(os.environ["DB_PATH"]).expanduser().resolve()
    # 3. DATABASE_URL
    url = os.environ.get("DATABASE_URL", "")
    if url:
        m = re.search(r"sqlite(?:\+\w+)?:///+(.*)", url)
        if m:
            return Path(m.group(1)).expanduser().resolve()
    # 4. scan a directory
    base = Path(os.environ.get("BOT_DATA_DIR", ".")).expanduser().resolve()
    candidates: list[Path] = []
    for pat in ("*.db", "*.sqlite", "*.sqlite3"):
        candidates += [Path(p) for p in glob.glob(str(base / pat))]
    candidates = [c for c in candidates if not c.name.endswith("-wal")
                  and not c.name.endswith("-shm")]
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        sys.exit(f"❌ No SQLite file found under {base}. "
                 f"Pass --db PATH or set DB_PATH / DATABASE_URL.")
    listing = "\n  ".join(str(c) for c in candidates)
    sys.exit(f"❌ Multiple SQLite files found; pick one with --db PATH:\n  {listing}")


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"❌ DB file does not exist: {path}")
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def list_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    return [r["name"] for r in rows]


def table_count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) AS c FROM {_quote_ident(table)}").fetchone()["c"]


def all_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {t: table_count(con, t) for t in list_tables(con)}


def integrity_ok(con: sqlite3.Connection) -> tuple[bool, str]:
    res = con.execute("PRAGMA integrity_check").fetchone()[0]
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    ok = (res == "ok") and not fk
    detail = res if res != "ok" else (f"foreign_key_check found {len(fk)} issue(s)"
                                      if fk else "ok")
    return ok, detail


# ======================================================================
# Commands
# ======================================================================
def cmd_discover(args):
    path = resolve_db_path(args.db)
    con = connect(path)
    try:
        print(f"📁 Database: {path}")
        print(f"   size: {path.stat().st_size / 1024:.1f} KB")
        print(f"   sha256: {_sha256(path)[:16]}…")
        ok, detail = integrity_ok(con)
        print(f"   integrity: {'✅ ok' if ok else '⚠️ ' + detail}")
        print("   tables:")
        for t in list_tables(con):
            cols = table_columns(con, t)
            print(f"     • {t}  ({table_count(con, t)} rows)")
            print(f"        cols: {', '.join(cols)}")
    finally:
        con.close()


def _backup_dir(args) -> Path:
    out = Path(args.out or os.environ.get("BOT_BACKUP_DIR", "backups"))
    out.mkdir(parents=True, exist_ok=True)
    return out.resolve()


def _consistent_snapshot(src_path: Path, dst_path: Path):
    """Online backup API → consistent even under WAL, even if bot is running."""
    src = sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _json_dump(con: sqlite3.Connection) -> dict:
    out = {"schema": "mcqbot-full-dump", "version": 1, "created_at": _now(),
           "tables": {}}
    for t in list_tables(con):
        cols = table_columns(con, t)
        rows = []
        for r in con.execute(f"SELECT * FROM {_quote_ident(t)}").fetchall():
            row = {}
            for c in cols:
                v = r[c]
                if isinstance(v, (bytes, bytearray)):
                    row[c] = {"__blob_b64__": base64.b64encode(bytes(v)).decode()}
                else:
                    row[c] = v
            rows.append(row)
        out["tables"][t] = {"columns": cols, "rows": rows}
    return out


def cmd_backup(args) -> Path:
    path = resolve_db_path(args.db)
    out = _backup_dir(args)
    stamp = _stamp()

    # 1. consistent .db snapshot
    snap = out / f"{path.stem}_{stamp}.db"
    _consistent_snapshot(path, snap)

    # 2. verify the snapshot
    scon = connect(snap)
    try:
        ok, detail = integrity_ok(scon)
        if not ok:
            snap.unlink(missing_ok=True)
            sys.exit(f"❌ Snapshot failed integrity check ({detail}); aborted.")
        counts = all_counts(scon)
        # 3. full JSON dump (human-readable, portable safety net)
        dump = _json_dump(scon)
    finally:
        scon.close()

    jpath = out / f"{path.stem}_{stamp}.json"
    jpath.write_text(json.dumps(dump, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    # 4. manifest with checksums + counts
    manifest = {
        "created_at": _now(),
        "source_db": str(path),
        "snapshot_db": str(snap),
        "snapshot_sha256": _sha256(snap),
        "json_dump": str(jpath),
        "json_sha256": _sha256(jpath),
        "row_counts": counts,
        "total_rows": sum(counts.values()),
    }
    mpath = out / f"{path.stem}_{stamp}.manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")

    print(f"✅ Backup complete ({sum(counts.values())} rows across "
          f"{len(counts)} tables)")
    print(f"   snapshot: {snap}")
    print(f"   json:     {jpath}")
    print(f"   manifest: {mpath}")
    for t, c in counts.items():
        print(f"      {t}: {c}")
    return snap


def cmd_verify(args):
    path = Path(args.path).resolve() if args.path else resolve_db_path(args.db)
    con = connect(path)
    try:
        ok, detail = integrity_ok(con)
        print(f"📁 {path}")
        print(f"   integrity: {'✅ ok' if ok else '⚠️ ' + detail}")
        counts = all_counts(con)
        for t, c in counts.items():
            print(f"   {t}: {c}")
        print(f"   total rows: {sum(counts.values())}")
        if not ok:
            sys.exit(2)
    finally:
        con.close()


def _plan_migration(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return a list of (description, sql) additive statements that are needed.
    Never returns anything destructive."""
    plan: list[tuple[str, str]] = []
    existing_tables = set(list_tables(con))

    # New tables (additive, IF NOT EXISTS).
    for name, sql in EXPECTED_TABLES.items():
        if name not in existing_tables:
            plan.append((f"create table {name}", sql))

    # Missing columns on EXISTING tables only.
    for table, cols in EXPECTED_COLUMNS.items():
        if table not in existing_tables:
            continue  # don't fabricate core tables; db.py owns them
        have = set(table_columns(con, table))
        for col, (ctype, default) in cols.items():
            if col in have:
                continue
            ddl = f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(col)} {ctype}"
            if default is not None:
                ddl += f" DEFAULT {default}"
            plan.append((f"add column {table}.{col}", ddl))

    # Indexes (additive, IF NOT EXISTS).
    existing_idx = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    for name, sql in EXPECTED_INDEXES.items():
        if name not in existing_idx:
            plan.append((f"create index {name}", sql))

    # Safety: refuse any forbidden verb.
    for desc, sql in plan:
        if _FORBIDDEN.search(sql):
            sys.exit(f"❌ Refusing destructive statement in plan: {desc}\n   {sql}")
    return plan


def cmd_migrate(args):
    path = resolve_db_path(args.db)
    con = connect(path)
    try:
        plan = _plan_migration(con)
    finally:
        con.close()

    if not plan:
        print("✅ Schema is already up to date — no changes needed.")
        return

    print(f"📋 Planned ADDITIVE changes ({len(plan)}):")
    for desc, sql in plan:
        print(f"   • {desc}")
        print(f"        {sql}")

    if not args.apply:
        print("\nℹ️ Dry run. Re-run with --apply to execute "
              "(a fresh backup is taken automatically first).")
        return

    # Backup BEFORE applying.
    print("\n💾 Taking a fresh backup before applying…")
    cmd_backup(args)

    before = None
    con = connect(path)
    try:
        before = all_counts(con)
        # Single atomic transaction: SQLite DDL is transactional, so any
        # failure rolls the whole thing back — never a half-migrated DB.
        try:
            con.execute("BEGIN")
            # ensure bookkeeping table exists first (it may be in the plan)
            con.execute(EXPECTED_TABLES["_schema_migrations"])
            for desc, sql in plan:
                if desc == "create table _schema_migrations":
                    continue  # already ensured above
                con.execute(sql)
                con.execute(
                    "INSERT INTO _schema_migrations(step, applied_at) VALUES (?, ?)",
                    (desc, _now()))
            con.execute("COMMIT")
        except Exception as e:
            con.execute("ROLLBACK")
            sys.exit(f"❌ Migration failed and was rolled back (DB unchanged): {e}")

        after = all_counts(con)
        ok, detail = integrity_ok(con)
        if not ok:
            sys.exit(f"⚠️ Post-migration integrity issue: {detail}")
        # Row-count guard: no table may have lost rows.
        for t, c in before.items():
            if after.get(t, 0) < c:
                sys.exit(f"❌ Row count dropped for {t}: {c} -> {after.get(t)}. "
                         f"Restore from the backup just taken.")
        print(f"\n✅ Migration applied atomically. Integrity ok. "
              f"No rows lost ({sum(after.values())} total).")
    finally:
        con.close()


def cmd_export_json(args):
    path = resolve_db_path(args.db)
    con = connect(path)
    try:
        dump = _json_dump(con)
    finally:
        con.close()
    out = Path(args.out).resolve()
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(t["rows"]) for t in dump["tables"].values())
    print(f"✅ Exported {total} rows ({len(dump['tables'])} tables) -> {out}")


def cmd_import_json(args):
    src = Path(getattr(args, "in")).resolve()
    target = Path(args.target).resolve()
    dump = json.loads(src.read_text(encoding="utf-8"))
    tables = dump.get("tables", {})

    con = sqlite3.connect(str(target))
    con.row_factory = sqlite3.Row
    try:
        existing = set(t["name"] for t in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        inserted = 0
        for tname, tdata in tables.items():
            if tname.startswith("sqlite_"):
                continue
            cols = tdata["columns"]
            if tname not in existing:
                # Recreate a permissive table so disaster-recovery works even
                # into a brand-new empty DB. (Never DROPs an existing one.)
                coldef = ", ".join(_quote_ident(c) for c in cols)
                con.execute(f"CREATE TABLE IF NOT EXISTS {_quote_ident(tname)} ({coldef})")
            placeholders = ", ".join("?" for _ in cols)
            collist = ", ".join(_quote_ident(c) for c in cols)
            verb = "INSERT OR REPLACE" if args.upsert else "INSERT OR IGNORE"
            stmt = f"{verb} INTO {_quote_ident(tname)} ({collist}) VALUES ({placeholders})"
            for row in tdata["rows"]:
                vals = []
                for c in cols:
                    v = row.get(c)
                    if isinstance(v, dict) and "__blob_b64__" in v:
                        v = base64.b64decode(v["__blob_b64__"])
                    vals.append(v)
                con.execute(stmt, vals)
                inserted += 1
        con.commit()
        print(f"✅ Imported up to {inserted} rows into {target} "
              f"({'upsert' if args.upsert else 'insert-or-ignore'}; never drops).")
    finally:
        con.close()


def cmd_restore(args):
    backup = Path(getattr(args, "from")).resolve()
    live = resolve_db_path(args.db)
    if not backup.exists():
        sys.exit(f"❌ Backup not found: {backup}")
    # Verify the backup first.
    bcon = connect(backup)
    try:
        ok, detail = integrity_ok(bcon)
        if not ok:
            sys.exit(f"❌ Backup failed integrity check ({detail}); not restoring.")
        bcounts = all_counts(bcon)
    finally:
        bcon.close()
    # Safety copy of the CURRENT live DB before overwriting (even if corrupt).
    if live.exists():
        safety = live.with_name(f"{live.stem}_pre-restore_{_stamp()}.db")
        shutil.copy2(live, safety)
        print(f"🛟 Saved current live DB to {safety}")
    # Write to a temp file on the same filesystem, then atomically replace.
    # The snapshot is already a complete, consistent .db, so a file copy is
    # sufficient and safe — and atomic replace means the live path is never
    # left half-written (and it works even if the old live file was corrupt).
    # NOTE: stop the bot before restoring so nothing holds the file open.
    tmp = live.with_name(f".{live.stem}_restore_{_stamp()}.tmp")
    shutil.copy2(backup, tmp)
    os.replace(tmp, live)
    print(f"✅ Restored {sum(bcounts.values())} rows -> {live}")


def cmd_predeploy(args):
    print("=== PRE-DEPLOY SAFETY RUN ===\n")
    path = resolve_db_path(args.db)
    print(f"Target DB: {path}\n")

    print("[1/4] Backup …")
    cmd_backup(args)

    print("\n[2/4] Verify live DB …")
    con = connect(path)
    try:
        ok, detail = integrity_ok(con)
        if not ok:
            sys.exit(f"❌ Live DB failed integrity check ({detail}); aborting deploy.")
        print(f"   ✅ integrity ok, {sum(all_counts(con).values())} rows")
    finally:
        con.close()

    print("\n[3/4] Migrate (additive, atomic) …")
    args.apply = True
    cmd_migrate(args)

    print("\n[4/4] Re-verify …")
    con = connect(path)
    try:
        ok, detail = integrity_ok(con)
        if not ok:
            sys.exit(f"❌ Post-migration integrity issue: {detail}")
        print(f"   ✅ integrity ok, {sum(all_counts(con).values())} rows")
    finally:
        con.close()

    print("\n🎉 Safe to deploy. A verified backup + JSON dump are in your "
          "backups directory; restore with:  python dbtools.py restore --from <snapshot.db>")


# ======================================================================
# CLI
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Safe backup & additive migration "
                                            "for the MCQ bot SQLite DB.")
    p.add_argument("--db", help="explicit path to the .db file")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover").set_defaults(func=cmd_discover)

    b = sub.add_parser("backup"); b.add_argument("--out"); b.set_defaults(func=cmd_backup)

    v = sub.add_parser("verify"); v.add_argument("path", nargs="?"); v.set_defaults(func=cmd_verify)

    m = sub.add_parser("migrate")
    m.add_argument("--apply", action="store_true")
    m.add_argument("--out")  # backup dir used when --apply
    m.set_defaults(func=cmd_migrate)

    e = sub.add_parser("export-json"); e.add_argument("--out", required=True)
    e.set_defaults(func=cmd_export_json)

    i = sub.add_parser("import-json")
    i.add_argument("--in", required=True)
    i.add_argument("--target", required=True)
    i.add_argument("--upsert", action="store_true",
                   help="overwrite rows with same PK (default: keep existing)")
    i.set_defaults(func=cmd_import_json)

    r = sub.add_parser("restore"); r.add_argument("--from", required=True)
    r.set_defaults(func=cmd_restore)

    pd = sub.add_parser("predeploy"); pd.add_argument("--out")
    pd.set_defaults(func=cmd_predeploy)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
