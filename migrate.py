"""
migrate.py — One-time migration: legacy JSON files → SQLite (via db.py)
========================================================================

Usage:
    python migrate.py [--data-dir PATH] [--dry-run]

This script is **idempotent**: running it again is a no-op for users that
already exist in the DB. It will:
    - Read allowed_users.json, user_settings.json, user_quotas.json,
      user_info.json, bot_state.json, admin_prefs.json
    - Upsert each user into the `users` table with merged data
    - Write bot_enabled + admin_prefs into the `bot_state` table

After a successful migration, the JSON files are NOT deleted — you can
move/delete them yourself once you've verified everything works:

    mv allowed_users.json allowed_users.json.bak
    ...

The bot itself will also auto-run this migration on startup if it finds
JSON files but an empty DB (see bot.py::auto_migrate_on_startup).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

import db


TIER_LIMITS = {"free": 3, "basic": 6, "pro": 10, "vip": 15}


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️  failed to read {path.name}: {e}")
        return default


async def run_migration(data_dir: Path, dry_run: bool = False) -> dict:
    await db.init_db()

    allowed   = set(int(x) for x in _load_json(data_dir / "allowed_users.json", []))
    settings  = _load_json(data_dir / "user_settings.json", {})
    quotas    = _load_json(data_dir / "user_quotas.json",   {})
    info      = _load_json(data_dir / "user_info.json",     {})
    bot_state = _load_json(data_dir / "bot_state.json",     {"enabled": True})
    admin_prefs = _load_json(data_dir / "admin_prefs.json", {"keyboard_hidden": False})

    all_ids: set[int] = set()
    all_ids.update(allowed)
    for d in (settings, quotas, info):
        if isinstance(d, dict):
            for k in d.keys():
                try:
                    all_ids.add(int(k))
                except ValueError:
                    pass

    print(f"Found {len(all_ids)} unique users to migrate "
          f"({len(allowed)} allowed, {len(info)} with info).")

    today = date.today().isoformat()
    created = updated = 0

    async with db.async_session_factory() as s:
        for uid in sorted(all_ids):
            sk = str(uid)
            u_info     = info.get(sk, {}) or {}
            u_settings = settings.get(sk, {}) or {}
            u_quota    = quotas.get(sk, {}) or {}

            tier        = u_quota.get("tier", "free")
            daily_limit = u_quota.get("daily_limit", TIER_LIMITS.get(tier, 3))
            used_today  = u_quota.get("used_today", 0)
            last_reset  = u_quota.get("last_reset_date", today)

            existing = await s.get(db.User, uid)
            payload = dict(
                username=u_info.get("username", "") or "",
                first_name=u_info.get("first_name", "") or "",
                last_name=u_info.get("last_name", "") or "",
                first_seen=u_info.get("first_seen", "") or "",
                last_seen=u_info.get("last_seen", "") or "",
                allowed=(uid in allowed),
                tier=tier,
                daily_limit=daily_limit,
                used_today=used_today,
                last_reset_date=last_reset,
                anonymous=bool(u_settings.get("anonymous", True)),
                protect_content=bool(u_settings.get("protect_content", False)),
                smart_chat_enabled=bool(u_settings.get("smart_chat_enabled", True)),
            )

            if existing is None:
                u = db.User(id=uid, **payload)
                s.add(u)
                created += 1
            else:
                for k, v in payload.items():
                    setattr(existing, k, v)
                updated += 1

        # Bot state — inline the upsert because the session is already open.
        await _upsert_state(s, "enabled", "1" if bot_state.get("enabled", True) else "0")
        await _upsert_state(s, "admin_keyboard_hidden",
                            "1" if admin_prefs.get("keyboard_hidden") else "0")

        if dry_run:
            print("  (dry-run) rolling back.")
            await s.rollback()
        else:
            await s.commit()

    result = {"created": created, "updated": updated, "total": len(all_ids)}
    print(f"\n✅ Migration result: created={created}, updated={updated}, "
          f"total={len(all_ids)}.")
    return result


async def _upsert_state(session, key: str, value: str):
    existing = await session.get(db.BotState, key)
    if existing is None:
        session.add(db.BotState(key=key, value=value))
    else:
        existing.value = value


def main():
    ap = argparse.ArgumentParser(description="Migrate JSON state files to SQLite.")
    ap.add_argument("--data-dir", default=".", help="Directory containing the JSON files")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report, but don't commit anything")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    print(f"Data dir: {data_dir}")
    print(f"DB:       {db.DATABASE_URL}\n")

    asyncio.run(run_migration(data_dir, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
