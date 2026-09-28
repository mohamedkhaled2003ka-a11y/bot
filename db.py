"""
db.py — SQLAlchemy 2.0 async database layer (SQLite + aiosqlite)
=================================================================

All persistent state for the bot lives here. The bot.py module talks to
this layer through async helpers; nothing else touches the engine.

Tables
------
- users           : everything per-user (info, settings, quota, tier, referral)
- generations     : audit log of every MCQ-generation request
- key_usage       : audit log of every successful/failed Gemini API call
- referrals       : who-referred-whom (one row per referred user)
- bot_state       : key-value store for global flags (enabled, admin prefs)

Switch to PostgreSQL by setting DATABASE_URL to a postgres+asyncpg DSN
and the SQLAlchemy models stay the same — only the engine URL changes.
"""

from __future__ import annotations

import os
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Integer, String, Boolean, Text, ForeignKey, select, func, update, delete, case,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine, AsyncEngine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ─────────────────────────────────────────────────────────────
# Engine / Session
# ─────────────────────────────────────────────────────────────
_DB_FILE = Path(os.environ.get("BOT_DB_PATH", "bot.db")).resolve()
_DB_FILE.parent.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{_DB_FILE}",
)

# echo=False for production. Pool args are SQLite-friendly defaults.
_engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
)

async_session_factory = async_sessionmaker(
    _engine, class_=AsyncSession, expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id:           Mapped[int]  = mapped_column(Integer, primary_key=True)
    username:     Mapped[str]  = mapped_column(String(64), default="")
    first_name:   Mapped[str]  = mapped_column(String(128), default="")
    last_name:    Mapped[str]  = mapped_column(String(128), default="")
    first_seen:   Mapped[str]  = mapped_column(String(32), default="")
    last_seen:    Mapped[str]  = mapped_column(String(32), default="", index=True)

    allowed:      Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tier:         Mapped[str]  = mapped_column(String(16), default="free")
    daily_limit:  Mapped[int]  = mapped_column(Integer, default=3)
    used_today:   Mapped[int]  = mapped_column(Integer, default=0)
    last_reset_date: Mapped[str] = mapped_column(String(16), default="")
    bonus_lectures:  Mapped[int] = mapped_column(Integer, default=0)
    total_generated: Mapped[int] = mapped_column(Integer, default=0)

    anonymous:           Mapped[bool] = mapped_column(Boolean, default=True)
    protect_content:     Mapped[bool] = mapped_column(Boolean, default=False)
    smart_chat_enabled:  Mapped[bool] = mapped_column(Boolean, default=True)

    # Referral system
    referred_by:    Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    referrals_made: Mapped[int] = mapped_column(Integer, default=0)


class Generation(Base):
    __tablename__ = "generations"

    id:           Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id:      Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    timestamp:    Mapped[str] = mapped_column(String(32), index=True)
    num_questions: Mapped[int] = mapped_column(Integer, default=0)
    source_type:  Mapped[str] = mapped_column(String(16), default="")     # pdf | image | text
    language:     Mapped[str] = mapped_column(String(8),  default="")
    difficulty:   Mapped[str] = mapped_column(String(16), default="")
    success:      Mapped[bool] = mapped_column(Boolean, default=True)


class KeyUsage(Base):
    __tablename__ = "key_usage"

    id:        Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key_index: Mapped[int] = mapped_column(Integer, index=True)
    timestamp: Mapped[str] = mapped_column(String(32), index=True)
    success:   Mapped[bool] = mapped_column(Boolean, default=True)
    error:     Mapped[str]  = mapped_column(String(128), default="")


class Referral(Base):
    __tablename__ = "referrals"

    id:          Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), index=True)
    referred_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), unique=True)
    timestamp:   Mapped[str] = mapped_column(String(32))


class BotState(Base):
    __tablename__ = "bot_state"

    key:   Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class PdfTokenBalance(Base):
    __tablename__ = "pdf_token_balances"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class VirtualPatientSession(Base):
    __tablename__ = "virtual_patient_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    case_id: Mapped[str] = mapped_column(String(64), default="")
    specialty: Mapped[str] = mapped_column(String(64), default="")
    difficulty: Mapped[str] = mapped_column(String(16), default="")
    case_json: Mapped[str] = mapped_column(Text, default="{}")
    history_json: Mapped[str] = mapped_column(Text, default="[]")
    examinations_json: Mapped[str] = mapped_column(Text, default="[]")
    investigations_json: Mapped[str] = mapped_column(Text, default="[]")
    started_at: Mapped[str] = mapped_column(String(32), default="")
    ended_at: Mapped[str] = mapped_column(String(32), default="")
    final_score: Mapped[int] = mapped_column(Integer, default=0)
    score_json: Mapped[str] = mapped_column(Text, default="{}")
    feedback: Mapped[str] = mapped_column(Text, default="")
    completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class RegisteredChat(Base):
    __tablename__ = "registered_chats"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    chat_type: Mapped[str] = mapped_column(String(32), default="")
    registered_at: Mapped[str] = mapped_column(String(32), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


# ─────────────────────────────────────────────────────────────
# Schema init
# ─────────────────────────────────────────────────────────────
async def init_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    await _engine.dispose()


# ─────────────────────────────────────────────────────────────
# Helpers — User / Settings / Quota
# ─────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _today_str() -> str:
    return date.today().isoformat()


async def get_or_create_user(tg_user) -> User:
    """Fetch or create a User row from a Telegram user object."""
    now = _now_iso()
    async with async_session_factory() as s:
        u = await s.get(User, tg_user.id)
        if u is None:
            u = User(
                id=tg_user.id,
                username=tg_user.username or "",
                first_name=tg_user.first_name or "",
                last_name=tg_user.last_name or "",
                first_seen=now,
                last_seen=now,
                last_reset_date=_today_str(),
            )
            s.add(u)
        else:
            u.username   = tg_user.username   or ""
            u.first_name = tg_user.first_name or ""
            u.last_name  = tg_user.last_name  or ""
            u.last_seen  = now
        await s.commit()
        await s.refresh(u)
        return u


async def get_user(user_id: int) -> Optional[User]:
    async with async_session_factory() as s:
        return await s.get(User, user_id)


async def update_user_info_if_missing(user_id: int, username: str = "",
                                      first_name: str = "", last_name: str = "") -> bool:
    """Backfill name/username for an existing user without touching last_seen.

    Used by the admin user-listing flow to fill in names for users that were
    added by ID but never opened the bot themselves. Only writes fields that
    are currently empty in the DB, so it won't overwrite real data.
    Returns True if anything was actually written.
    """
    if not (username or first_name or last_name):
        return False
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return False
        changed = False
        if username and not u.username:
            u.username = username
            changed = True
        if first_name and not u.first_name:
            u.first_name = first_name
            changed = True
        if last_name and not u.last_name:
            u.last_name = last_name
            changed = True
        if changed:
            await s.commit()
        return changed


async def is_allowed(user_id: int, admin_ids: set[int]) -> bool:
    if user_id in admin_ids:
        return True
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        return bool(u and u.allowed)


async def allow_user(user_id: int):
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            u = User(
                id=user_id,
                first_seen=_now_iso(),
                last_seen=_now_iso(),
                last_reset_date=_today_str(),
                allowed=True,
            )
            s.add(u)
        else:
            u.allowed = True
        await s.commit()


async def revoke_user(user_id: int):
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u:
            u.allowed = False
            await s.commit()


async def list_allowed_user_ids() -> list[int]:
    async with async_session_factory() as s:
        rows = (await s.execute(
            select(User.id).where(User.allowed == True).order_by(User.id)
        )).scalars().all()
        return list(rows)


async def register_chat(chat_id: int, owner_user_id: int, title: str,
                        chat_type: str) -> dict:
    async with async_session_factory() as s:
        row = await s.get(RegisteredChat, chat_id)
        if row is None:
            row = RegisteredChat(
                chat_id=chat_id,
                owner_user_id=owner_user_id,
                title=title[:256],
                chat_type=chat_type[:32],
                registered_at=_now_iso(),
                enabled=False,
            )
            s.add(row)
        else:
            row.owner_user_id = owner_user_id
            row.title = title[:256]
            row.chat_type = chat_type[:32]
        await s.commit()
        return {
            "chat_id": row.chat_id,
            "owner_user_id": row.owner_user_id,
            "title": row.title,
            "chat_type": row.chat_type,
            "registered_at": row.registered_at,
            "enabled": row.enabled,
        }


async def list_registered_chats(enabled_only: bool = False) -> list[dict]:
    async with async_session_factory() as s:
        query = select(RegisteredChat).order_by(RegisteredChat.registered_at.desc())
        if enabled_only:
            query = query.where(RegisteredChat.enabled == True)
        rows = (await s.execute(query)).scalars().all()
        return [{
            "chat_id": row.chat_id,
            "owner_user_id": row.owner_user_id,
            "title": row.title,
            "chat_type": row.chat_type,
            "registered_at": row.registered_at,
            "enabled": row.enabled,
        } for row in rows]


async def set_chat_enabled(chat_id: int, enabled: bool) -> bool:
    async with async_session_factory() as s:
        row = await s.get(RegisteredChat, chat_id)
        if row is None:
            return False
        row.enabled = bool(enabled)
        await s.commit()
        return True


async def is_chat_enabled(chat_id: int) -> bool:
    async with async_session_factory() as s:
        row = await s.get(RegisteredChat, chat_id)
        return bool(row and row.enabled)


async def count_allowed_users() -> int:
    async with async_session_factory() as s:
        return int((await s.execute(
            select(func.count()).select_from(User).where(User.allowed == True)
        )).scalar_one())


async def set_setting(user_id: int, key: str, value):
    valid = {"anonymous", "protect_content", "smart_chat_enabled"}
    if key not in valid:
        raise ValueError(f"unknown setting: {key}")
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return
        setattr(u, key, bool(value))
        await s.commit()


async def reset_today_if_needed(user_id: int) -> User | None:
    """Reset used_today if last_reset_date is stale. Returns the (refreshed) user."""
    today = _today_str()
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return None
        if u.last_reset_date != today:
            u.used_today = 0
            u.last_reset_date = today
            await s.commit()
            await s.refresh(u)
        return u


async def can_consume(user_id: int) -> tuple[bool, int, int, int]:
    """
    Returns (allowed_to_proceed, daily_remaining, bonus_remaining, daily_limit).
    A user can proceed if they have at least 1 daily OR 1 bonus lecture left.
    """
    u = await reset_today_if_needed(user_id)
    if u is None:
        return False, 0, 0, 0
    daily_remaining = max(0, u.daily_limit - u.used_today)
    return (daily_remaining + u.bonus_lectures) > 0, daily_remaining, u.bonus_lectures, u.daily_limit


async def consume(user_id: int):
    """Decrement quota — daily first, then bonus. Increments total_generated."""
    today = _today_str()
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return
        if u.last_reset_date != today:
            u.used_today = 0
            u.last_reset_date = today
        if u.used_today < u.daily_limit:
            u.used_today += 1
        elif u.bonus_lectures > 0:
            u.bonus_lectures -= 1
        u.total_generated += 1
        await s.commit()


async def set_tier(user_id: int, tier: str, daily_limit: int):
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return
        u.tier = tier
        u.daily_limit = daily_limit
        await s.commit()


async def reset_usage(user_id: int):
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return
        u.used_today = 0
        u.last_reset_date = _today_str()
        await s.commit()


async def add_bonus(user_id: int, amount: int):
    async with async_session_factory() as s:
        u = await s.get(User, user_id)
        if u is None:
            return
        u.bonus_lectures = max(0, u.bonus_lectures + amount)
        await s.commit()


# ─────────────────────────────────────────────────────────────
# Referral helpers
# ─────────────────────────────────────────────────────────────
async def record_referral(referrer_id: int, referred_id: int) -> bool:
    """Returns True if a new referral was recorded, False if it already existed
    or is self-referral."""
    if referrer_id == referred_id:
        return False
    async with async_session_factory() as s:
        # Check if referred user already has a referrer
        ref = await s.get(User, referred_id)
        if ref is None or ref.referred_by is not None:
            return False
        existing = (await s.execute(
            select(Referral).where(Referral.referred_id == referred_id)
        )).scalar_one_or_none()
        if existing is not None:
            return False
        referrer = await s.get(User, referrer_id)
        if referrer is None:
            return False
        ref.referred_by = referrer_id
        referrer.referrals_made += 1
        s.add(Referral(
            referrer_id=referrer_id,
            referred_id=referred_id,
            timestamp=_now_iso(),
        ))
        await s.commit()
        return True


async def count_referrals_by(referrer_id: int) -> int:
    async with async_session_factory() as s:
        return int((await s.execute(
            select(func.count()).select_from(Referral)
            .where(Referral.referrer_id == referrer_id)
        )).scalar_one())


# ─────────────────────────────────────────────────────────────
# Bot state (global flags)
# ─────────────────────────────────────────────────────────────
async def get_state(key: str, default: str = "") -> str:
    async with async_session_factory() as s:
        row = await s.get(BotState, key)
        return row.value if row else default


async def set_state(key: str, value: str):
    async with async_session_factory() as s:
        row = await s.get(BotState, key)
        if row is None:
            s.add(BotState(key=key, value=value))
        else:
            row.value = value
        await s.commit()


# ─────────────────────────────────────────────────────────────
# AI PDF restyling tokens
# ─────────────────────────────────────────────────────────────
async def get_pdf_tokens(user_id: int) -> int:
    async with async_session_factory() as s:
        row = await s.get(PdfTokenBalance, user_id)
        return max(0, int(row.tokens)) if row else 0


async def add_pdf_tokens(user_id: int, amount: int) -> int:
    if amount == 0:
        return await get_pdf_tokens(user_id)
    async with async_session_factory() as s:
        row = await s.get(PdfTokenBalance, user_id)
        if row is None:
            row = PdfTokenBalance(user_id=user_id, tokens=0)
            s.add(row)
        row.tokens = max(0, int(row.tokens) + int(amount))
        await s.commit()
        return row.tokens


async def consume_pdf_tokens(user_id: int, amount: int) -> tuple[bool, int]:
    """Consume tokens atomically enough for the single-worker SQLite flow."""
    if amount <= 0:
        return True, await get_pdf_tokens(user_id)
    async with async_session_factory() as s:
        row = await s.get(PdfTokenBalance, user_id)
        if row is None or row.tokens < amount:
            return False, max(0, int(row.tokens)) if row else 0
        row.tokens -= amount
        await s.commit()
        return True, row.tokens


# ─────────────────────────────────────────────────────────────
# Virtual patient sessions and score history
# ─────────────────────────────────────────────────────────────
def _json_loads(raw: str, fallback):
    try:
        value = json.loads(raw or "")
        return value if value is not None else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _session_dict(row: VirtualPatientSession | None) -> dict | None:
    if row is None:
        return None
    return {
        "user_id": row.user_id,
        "case_id": row.case_id,
        "specialty": row.specialty,
        "difficulty": row.difficulty,
        "case": _json_loads(row.case_json, {}),
        "history": _json_loads(row.history_json, []),
        "examinations": _json_loads(row.examinations_json, []),
        "investigations": _json_loads(row.investigations_json, []),
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "final_score": row.final_score,
        "score": _json_loads(row.score_json, {}),
        "feedback": row.feedback,
        "completed": row.completed,
    }


async def save_virtual_patient_session(user_id: int, session: dict):
    async with async_session_factory() as s:
        session_id = session.get("session_id")
        row = await s.get(VirtualPatientSession, int(session_id)) if session_id else None
        if row is None and session.get("case_id"):
            row = (await s.execute(
                select(VirtualPatientSession)
                .where(VirtualPatientSession.user_id == user_id,
                       VirtualPatientSession.case_id == str(session["case_id"]))
                .order_by(VirtualPatientSession.id.desc())
                .limit(1)
            )).scalar_one_or_none()
        if row is None and not session.get("completed", False):
            row = (await s.execute(
                select(VirtualPatientSession)
                .where(VirtualPatientSession.user_id == user_id,
                       VirtualPatientSession.completed == False)
                .order_by(VirtualPatientSession.started_at.desc())
                .limit(1)
            )).scalar_one_or_none()
        values = {
            "case_id": str(session.get("case_id", "")),
            "specialty": str(session.get("specialty", "")),
            "difficulty": str(session.get("difficulty", "")),
            "case_json": json.dumps(session.get("case", {}), ensure_ascii=False),
            "history_json": json.dumps(session.get("history", []), ensure_ascii=False),
            "examinations_json": json.dumps(session.get("examinations", []), ensure_ascii=False),
            "investigations_json": json.dumps(session.get("investigations", []), ensure_ascii=False),
            "started_at": str(session.get("started_at", "")),
            "ended_at": str(session.get("ended_at", "")),
            "final_score": int(session.get("final_score", 0) or 0),
            "score_json": json.dumps(session.get("score", {}), ensure_ascii=False),
            "feedback": str(session.get("feedback", "")),
            "completed": bool(session.get("completed", False)),
        }
        if row is None:
            s.add(VirtualPatientSession(user_id=user_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await s.commit()
        if row is not None:
            session["session_id"] = row.id


async def get_virtual_patient_session(user_id: int, active_only: bool = False) -> dict | None:
    async with async_session_factory() as s:
        query = select(VirtualPatientSession).where(
            VirtualPatientSession.user_id == user_id)
        if active_only:
            query = query.where(VirtualPatientSession.completed == False)
        row = (await s.execute(
            query.order_by(VirtualPatientSession.started_at.desc()).limit(1)
        )).scalar_one_or_none()
        if row is None:
            return None
        result = _session_dict(row)
        if result is not None:
            result["session_id"] = row.id
        return result


async def list_virtual_patient_scores(user_id: int, limit: int = 10) -> list[dict]:
    async with async_session_factory() as s:
        rows = (await s.execute(
            select(VirtualPatientSession)
            .where(VirtualPatientSession.user_id == user_id,
                   VirtualPatientSession.completed == True)
            .order_by(VirtualPatientSession.ended_at.desc())
            .limit(limit)
        )).scalars().all()
        results = []
        for row in rows:
            item = _session_dict(row)
            if item is not None:
                item["session_id"] = row.id
                results.append(item)
        return results


# ─────────────────────────────────────────────────────────────
# Audit logs
# ─────────────────────────────────────────────────────────────
async def log_generation(user_id: int, num_questions: int, source_type: str,
                         language: str, difficulty: str, success: bool = True):
    async with async_session_factory() as s:
        s.add(Generation(
            user_id=user_id,
            timestamp=_now_iso(),
            num_questions=num_questions,
            source_type=source_type,
            language=language,
            difficulty=difficulty,
            success=success,
        ))
        await s.commit()


async def log_key_usage(key_index: int, success: bool, error: str = ""):
    async with async_session_factory() as s:
        s.add(KeyUsage(
            key_index=key_index,
            timestamp=_now_iso(),
            success=success,
            error=(error or "")[:128],
        ))
        await s.commit()


# ─────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────
async def stats_today() -> dict:
    today_prefix = _today_str()
    async with async_session_factory() as s:
        gens_today = int((await s.execute(
            select(func.count()).select_from(Generation)
            .where(Generation.timestamp.like(f"{today_prefix}%"))
        )).scalar_one())
        questions_today = int((await s.execute(
            select(func.coalesce(func.sum(Generation.num_questions), 0))
            .where(Generation.timestamp.like(f"{today_prefix}%"))
        )).scalar_one())
        active_users_today = int((await s.execute(
            select(func.count(func.distinct(Generation.user_id)))
            .where(Generation.timestamp.like(f"{today_prefix}%"))
        )).scalar_one())
        return {
            "generations":  gens_today,
            "questions":    questions_today,
            "active_users": active_users_today,
        }


async def stats_window(days: int) -> dict:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    async with async_session_factory() as s:
        gens = int((await s.execute(
            select(func.count()).select_from(Generation)
            .where(Generation.timestamp >= cutoff)
        )).scalar_one())
        questions = int((await s.execute(
            select(func.coalesce(func.sum(Generation.num_questions), 0))
            .where(Generation.timestamp >= cutoff)
        )).scalar_one())
        return {"generations": gens, "questions": questions}


async def top_users(limit: int = 5, days: int = 7) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    async with async_session_factory() as s:
        rows = (await s.execute(
            select(
                Generation.user_id,
                func.count().label("cnt"),
                func.coalesce(func.sum(Generation.num_questions), 0).label("q"),
            )
            .where(Generation.timestamp >= cutoff)
            .group_by(Generation.user_id)
            .order_by(func.count().desc())
            .limit(limit)
        )).all()
        results = []
        for uid, cnt, q in rows:
            u = await s.get(User, uid)
            results.append({
                "user_id":    uid,
                "username":   u.username if u else "",
                "first_name": u.first_name if u else "",
                "lectures":   int(cnt),
                "questions":  int(q),
            })
        return results


async def key_stats(days: int = 1) -> list[dict]:
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    async with async_session_factory() as s:
        rows = (await s.execute(
            select(
                KeyUsage.key_index,
                func.sum(case((KeyUsage.success == True, 1), else_=0)).label("ok"),
                func.sum(case((KeyUsage.success == False, 1), else_=0)).label("fail"),
            )
            .where(KeyUsage.timestamp >= cutoff)
            .group_by(KeyUsage.key_index)
            .order_by(KeyUsage.key_index)
        )).all()
        return [{"key_index": int(k), "ok": int(ok or 0), "fail": int(fail or 0)}
                for k, ok, fail in rows]

# ─────────────────────────────────────────────────────────────
# Admin paginated listing
# ─────────────────────────────────────────────────────────────
async def list_users_page(page: int, per_page: int) -> tuple[list[User], int]:
    offset = page * per_page
    async with async_session_factory() as s:
        total = int((await s.execute(
            select(func.count()).select_from(User).where(User.allowed == True)
        )).scalar_one())
        rows = (await s.execute(
            select(User)
            .where(User.allowed == True)
            .order_by(User.id)
            .offset(offset).limit(per_page)
        )).scalars().all()
        return list(rows), total


async def search_allowed_users(query: str, limit: int = 20) -> list[User]:
    """Find allowed users by Telegram ID, username, or display name."""
    query = (query or "").strip()
    if not query:
        return []
    async with async_session_factory() as s:
        if query.isdigit():
            rows = (await s.execute(
                select(User).where(User.allowed == True, User.id == int(query))
            )).scalars().all()
        else:
            pattern = f"%{query}%"
            rows = (await s.execute(
                select(User).where(
                    User.allowed == True,
                    (User.username.ilike(pattern)
                     | User.first_name.ilike(pattern)
                     | User.last_name.ilike(pattern)),
                ).order_by(User.first_name, User.username, User.id).limit(limit)
            )).scalars().all()
        return list(rows[:limit])
