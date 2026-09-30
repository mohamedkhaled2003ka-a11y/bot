"""
Telegram MCQ Bot — Gemini Edition (v5)
======================================

What's new in v5
----------------
1. Backup / Restore of all users (admin) + guidance to put the DB on a
   persistent volume so users survive redeploys.            [addons.py]
2. True concurrent processing: `concurrent_updates(True)` so one big PDF no
   longer blocks every other user.
3. Large question sets (100/200+) via batched generation.   [addons.py]
4. Smart Mode OFF by default for new users + admin dashboard / bulk toggle.
5. Quiz generation from an uploaded question file (PDF/DOCX/TXT). [addons.py]
6. Spot Diagnosis mode (image -> identification, spoiler reveal). [addons.py]
7. Islamic section: Azkar, full Quran reading, multi-reciter audio, search,
   bookmarks.                                                [islamic.py]
8. /health and /tasks monitoring; long jobs run as tracked background tasks.

(v4.x history retained below in module comments.)

Requires:
    pip install -r requirements.txt   # incl. python-docx, httpx
"""

from __future__ import annotations

import os
import re
import json
import random
import asyncio
import logging
import time
from io import BytesIO
from pathlib import Path
from datetime import datetime, date
from typing import Optional

import pdfplumber
import fitz  # PyMuPDF
from google import genai
from google.genai import types
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputFile,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ApplicationHandlerStop,
    filters,
)

import db
import export as exporter
from migrate import run_migration as run_json_migration

# v5 feature modules (drop-in; they import `bot` lazily to avoid cycles)
import addons
import islamic

# ============================================================
# Configuration
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
BOT_USERNAME   = os.environ.get("BOT_USERNAME", "").lstrip("@")  # for referral links


def _load_gemini_keys() -> list[str]:
    """Load Gemini keys from env. Accepts both ``GEMINI_API_KEYS`` (plural,
    intended for multiple) and ``GEMINI_API_KEY`` (singular). In *either* var,
    keys may be separated by commas, semicolons, newlines, or whitespace —
    Gemini keys themselves don't contain any of these characters, so this is
    safe and works no matter which variable a user dropped a list into.
    Duplicates are removed; final list is capped at 20.
    """
    keys: list[str] = []
    for var in ("GEMINI_API_KEYS", "GEMINI_API_KEY"):
        raw = os.environ.get(var, "")
        if not raw:
            continue
        for k in re.split(r"[,\s;]+", raw):
            k = k.strip()
            if not k:
                continue
            # Gemini API keys are API-key credentials (typically start with
            # "AIza"). Do not pass OAuth access tokens such as "AQ..." to
            # genai.Client(api_key=...), because Google rejects them with
            # 401 ACCESS_TOKEN_TYPE_UNSUPPORTED.
            if not k.startswith("AIza"):
                print(
                    f"WARNING: Ignoring a non-API-key credential in {var}. "
                    "Use Gemini API keys from Google AI Studio (typically "
                    "starting with 'AIza'), not OAuth access tokens."
                )
                continue
            if k not in keys:
                keys.append(k)
    return keys[:20]


GEMINI_KEYS    = _load_gemini_keys()
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

ADMIN_IDS = {
    8015150141,  # MK
}
ADMIN_USERNAME = "@VOLDYI"

# Referral reward (lectures granted to BOTH sides on a successful referral)
REFERRAL_REWARD = int(os.environ.get("REFERRAL_REWARD", "3"))

DIFFICULTY_INSTRUCTIONS = {
    "easy":   "Make questions EASY: focus on direct recall of definitions, simple facts, and basic concepts. Distractors should be plausible but distinguishable.",
    "medium": "Make questions MEDIUM difficulty: a balanced mix of recall, understanding, and simple application. Distractors should be reasonably tricky.",
    "hard":   "Make questions HARD: focus on application, analysis, comparison, and reasoning. Distractors should be subtle and require careful thinking.",
    "mixed":  "Vary the difficulty within the set: roughly 30% easy (recall), 40% medium (understanding/application), 30% hard (analysis/reasoning).",
}

DIFFICULTY_LABELS = {
    "easy":   "سهل 🟢",
    "medium": "متوسط 🟡",
    "hard":   "صعب 🔴",
    "mixed":  "متنوع 🎲",
}

LANG_INSTRUCTIONS = {
    "auto": "Match the language of the source: Arabic content -> Arabic questions, English content -> English questions, mixed -> use the dominant language.",
    "ar":   "Write ALL questions and options in ARABIC, even if the source is in English. You may keep highly technical/medical English terms in parentheses where translation would be awkward.",
    "en":   "Write ALL questions and options in ENGLISH, even if the source is in Arabic. Translate Arabic content to clear, accurate English.",
}

LANG_LABELS = {
    "auto": "تلقائي 🌐",
    "ar":   "عربي 🇪🇬",
    "en":   "إنجليزي 🇬🇧",
}

TIERS = {
    "free":  {"label": "مجاني 🆓",  "daily_limit": 3},
    "basic": {"label": "أساسي ⭐",   "daily_limit": 6},
    "pro":   {"label": "احترافي 💎", "daily_limit": 10},
    "vip":   {"label": "VIP 👑",     "daily_limit": 15},
}

MAX_PDF_CHARS        = 120_000
MAX_QUESTION_LEN     = 300
MAX_OPTION_LEN       = 100
MAX_SMART_REPLY      = 3500
MIN_QUESTIONS        = 1
MAX_QUESTIONS        = 200
MAX_IMAGES_PER_PDF   = 20
MIN_IMAGE_BYTES      = 6_000
MAX_OUTPUT_TOKENS    = 8192
MAX_FILE_SIZE        = 20 * 1024 * 1024
MAX_PDF_PAGES        = 100
MAX_MULTI_ADD        = 100        # max IDs accepted in one "Multiple Add" op
USERS_PER_PAGE       = 10
RATE_LIMIT_SECONDS   = 3.0
STATE_TIMEOUT        = 600
GEMINI_MAX_RETRIES   = 3          # transient retries PER KEY (not total)
GEMINI_KEY_COOLDOWN  = 60
BROADCAST_DELAY      = 0.05
LAST_MCQS_CACHE      = "last_mcqs"

# Webhook config
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_LISTEN = os.environ.get("WEBHOOK_LISTEN", "0.0.0.0")
WEBHOOK_PORT   = int(os.environ.get("WEBHOOK_PORT", "8443"))
WEBHOOK_PATH   = os.environ.get("WEBHOOK_PATH", "telegram")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip() or None

# ============================================================
# Reply Keyboard Labels
# ============================================================
BTN_QUIZ_MODE     = "📝 عمل كويز MCQ"
BTN_SMART_CHAT    = "🤖 الوضع الذكي"
BTN_MY_INFO       = "👤 بياناتي"
BTN_MY_SETTINGS   = "⚙️ إعداداتي"
BTN_REFERRAL      = "🎁 ادعو أصحابك"
BTN_HELP          = "ℹ️ مساعدة"
BTN_START_OVER    = "🏠 الرئيسية"

# v5 user-facing features
BTN_SPOT          = "🔍 تشخيص صورة"
BTN_QFILE         = "📋 كويز من ملف"
BTN_ISLAMIC       = "🕌 إسلامي"
BTN_MEDICAL       = "🩺 تشخيص طبي"
BTN_CLINICAL      = "🧠 حالات إكلينيكية"
BTN_VIRTUAL_PATIENT = "🩺 مريض افتراضي"

BTN_ADMIN_PANEL   = "🎛️ لوحة التحكم"
BTN_MANAGE_USERS  = "👥 إدارة المستخدمين"
BTN_MANAGE_CHATS  = "💬 إدارة المحادثات الطبية"
BTN_MULTI_ADD     = "➕👥 إضافة جماعية"   # Multiple Add
BTN_TOGGLE_BOT    = "🔄 تشغيل/إيقاف"
BTN_BOT_STATUS    = "📊 الحالة"
BTN_ANALYTICS     = "📈 إحصائيات"
BTN_BROADCAST     = "📢 رسالة جماعية"
BTN_MEDIA_BROADCAST = "📸🎬 إرسال صورة/فيديو"
BTN_HIDE_ADMIN    = "🙈 إخفاء لوحة الأدمن"
BTN_SHOW_ADMIN    = "👁️ إظهار لوحة الأدمن"
BTN_HIDE_FEATURES = "🙈 إخفاء الأدوات"
BTN_SHOW_FEATURES = "👁️ إظهار الأدوات"
BTN_PDF_RESTYLE = "✨ إعادة تنسيق PDF"
BTN_TOGGLE_PDF_RESTYLE = "✨ PDF AI تشغيل/إيقاف"
BTN_PDF_TOKENS = "🎟️ رصيد PDF"
BTN_PDF_TOKEN_COST = "💰 تكلفة PDF"

# v5 admin features
BTN_BACKUP        = "💾 نسخة احتياطية"
BTN_RESTORE       = "♻️ استعادة"
BTN_SMARTDASH     = "🤖 لوحة الذكي"
BTN_TOGGLE_VIRTUAL_PATIENT = "🩺 المريض الافتراضي تشغيل/إيقاف"

BTN_EXIT_CHAT     = "🚪 الخروج من المحادثة"
BTN_CLEAR_HISTORY = "🧹 مسح المحادثة"
BTN_CANCEL        = "❌ إلغاء"

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logger.info("Loaded %d Gemini API key(s)", len(GEMINI_KEYS))


# ============================================================
# Cached global flags (DB is source of truth; cache for hot reads)
# ============================================================
_BOT_ENABLED: bool = True
_ADMIN_KB_HIDDEN: bool = False
_FEATURES_KB_HIDDEN: bool = False
_PDF_RESTYLE_ENABLED: bool = False
_PDF_RESTYLE_COST: int = 1
_VIRTUAL_PATIENT_ENABLED: bool = False


async def _load_flags():
    global _BOT_ENABLED, _ADMIN_KB_HIDDEN, _FEATURES_KB_HIDDEN, _PDF_RESTYLE_ENABLED, _PDF_RESTYLE_COST, _VIRTUAL_PATIENT_ENABLED
    _BOT_ENABLED     = (await db.get_state("enabled", "1")) == "1"
    _ADMIN_KB_HIDDEN = (await db.get_state("admin_keyboard_hidden", "0")) == "1"
    _FEATURES_KB_HIDDEN = (await db.get_state("features_keyboard_hidden", "0")) == "1"
    _PDF_RESTYLE_ENABLED = (await db.get_state("pdf_restyle_enabled", "0")) == "1"
    try:
        _PDF_RESTYLE_COST = max(1, int(await db.get_state("pdf_restyle_cost", "1")))
    except ValueError:
        _PDF_RESTYLE_COST = 1
    _VIRTUAL_PATIENT_ENABLED = (await db.get_state("virtual_patient_enabled", "0")) == "1"


async def _set_bot_enabled(value: bool):
    global _BOT_ENABLED
    _BOT_ENABLED = value
    await db.set_state("enabled", "1" if value else "0")


async def _set_admin_kb_hidden(value: bool):
    global _ADMIN_KB_HIDDEN
    _ADMIN_KB_HIDDEN = value
    await db.set_state("admin_keyboard_hidden", "1" if value else "0")


async def _set_features_kb_hidden(value: bool):
    global _FEATURES_KB_HIDDEN
    _FEATURES_KB_HIDDEN = value
    await db.set_state("features_keyboard_hidden", "1" if value else "0")


async def _set_pdf_restyle_enabled(value: bool):
    global _PDF_RESTYLE_ENABLED
    _PDF_RESTYLE_ENABLED = value
    await db.set_state("pdf_restyle_enabled", "1" if value else "0")


async def _set_pdf_restyle_cost(value: int):
    global _PDF_RESTYLE_COST
    _PDF_RESTYLE_COST = max(1, min(100, int(value)))
    await db.set_state("pdf_restyle_cost", str(_PDF_RESTYLE_COST))


async def _set_virtual_patient_enabled(value: bool):
    global _VIRTUAL_PATIENT_ENABLED
    _VIRTUAL_PATIENT_ENABLED = value
    await db.set_state("virtual_patient_enabled", "1" if value else "0")


# ============================================================
# HTML escaping
# ============================================================
def html_escape(text) -> str:
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ============================================================
# Bulk-ID parsing (shared by the "Multiple Add" admin flow)
# ============================================================
def parse_id_list(raw: str) -> "tuple[list[int], list[str]]":
    """Parse a free-form blob of Telegram user IDs.

    IDs may be separated by commas, semicolons, whitespace, or newlines —
    mirroring how ``_load_gemini_keys`` splits its input. Returns a tuple
    ``(valid_ids, invalid_tokens)`` where:
      * ``valid_ids``      – unique positive integers, original order kept.
      * ``invalid_tokens`` – tokens that weren't a positive integer.
    """
    valid: list[int] = []
    invalid: list[str] = []
    seen: set[int] = set()
    for tok in re.split(r"[,\s;]+", (raw or "").strip()):
        tok = tok.strip()
        if not tok:
            continue
        try:
            uid = int(tok)
        except ValueError:
            invalid.append(tok)
            continue
        if uid <= 0:
            invalid.append(tok)
            continue
        if uid in seen:
            continue
        seen.add(uid)
        valid.append(uid)
    return valid, invalid


# ============================================================
# Gemini API Key Rotation Pool
# ============================================================
class GeminiKeyPool:
    """Round-robin pool of Gemini API clients.

    Keys that hit a rate-limit / quota error are placed on a cooldown and
    skipped until the cooldown expires. ``next_available`` advances the
    pointer and returns the next usable key (excluding any caller-specified
    set). If every key is cooling down, ``soonest_key`` tells the caller
    which one frees up first so it can wait instead of failing.
    """

    def __init__(self, keys: list[str]):
        if not keys:
            raise RuntimeError("No Gemini API keys configured")
        self.keys = list(keys)
        self.clients = [genai.Client(api_key=k) for k in self.keys]
        self.cooldown_until: dict[int, float] = {}
        self.current = 0
        self.lock = asyncio.Lock()

    def __len__(self):
        return len(self.clients)

    async def next_available(self, exclude: "set[int] | None" = None):
        """Return (idx, client) for the next key that is NOT on cooldown and
        NOT in ``exclude``. Advances the round-robin pointer. Returns None if
        every key is either excluded or currently cooling down."""
        exclude = exclude or set()
        async with self.lock:
            now = time.time()
            n = len(self.clients)
            for _ in range(n):
                idx = self.current
                self.current = (self.current + 1) % n
                if idx in exclude:
                    continue
                if self.cooldown_until.get(idx, 0) <= now:
                    return idx, self.clients[idx]
            return None

    async def soonest_key(self, exclude: "set[int] | None" = None):
        """Fallback when all keys are cooling down: return
        (idx, client, wait_seconds) for the key whose cooldown expires
        soonest, so the caller can wait for it. Returns None if everything
        is excluded."""
        exclude = exclude or set()
        async with self.lock:
            now = time.time()
            candidates = [i for i in range(len(self.clients)) if i not in exclude]
            if not candidates:
                return None
            idx = min(candidates, key=lambda i: self.cooldown_until.get(i, 0))
            wait = max(0.0, self.cooldown_until.get(idx, 0) - now)
            return idx, self.clients[idx], wait

    def cool(self, idx: int, seconds: int = GEMINI_KEY_COOLDOWN):
        self.cooldown_until[idx] = time.time() + seconds


KEY_POOL: Optional[GeminiKeyPool] = None
if GEMINI_KEYS:
    KEY_POOL = GeminiKeyPool(GEMINI_KEYS)


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "429", "rate", "quota", "exhausted", "resource_exhausted", "resourceexhausted",
    ))


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in (
        "500", "502", "503", "504", "unavailable", "deadline", "timeout",
        "internal", "overloaded",
    ))


async def call_gemini(**kwargs):
    """Call Gemini, rotating through ALL configured keys.

    Behaviour:
      * Pick the next available (non-cooling) key.
      * On a rate-limit / quota error: cool that key and IMMEDIATELY switch
        to the next key (don't waste retries on a dead key).
      * On a transient error (5xx / timeout / overloaded): retry the SAME
        key up to GEMINI_MAX_RETRIES times with a short backoff.
      * On any other error: move on to the next key.
      * If every remaining key is cooling down, wait for the soonest one to
        free up instead of giving up.
      * Only raise once every key has been exhausted.
    """
    if KEY_POOL is None:
        raise RuntimeError("Gemini key pool not initialized")

    last_exc: Optional[Exception] = None
    tried_keys: set[int] = set()
    n_keys = len(KEY_POOL)

    while len(tried_keys) < n_keys:
        picked = await KEY_POOL.next_available(exclude=tried_keys)

        if picked is None:
            # All not-yet-tried keys are on cooldown → wait for the soonest.
            soon = await KEY_POOL.soonest_key(exclude=tried_keys)
            if soon is None:
                break
            idx, client, wait = soon
            if wait > 0:
                logger.info("All keys cooling down; waiting %.1fs for key #%d",
                            wait, idx)
                await asyncio.sleep(min(wait, GEMINI_KEY_COOLDOWN))
        else:
            idx, client = picked

        # Per-key transient retries.
        transient_attempts = 0
        while True:
            try:
                resp = await client.aio.models.generate_content(**kwargs)
                asyncio.create_task(db.log_key_usage(idx, success=True))
                return resp
            except Exception as e:
                last_exc = e
                err_short = f"{type(e).__name__}: {str(e)[:80]}"
                asyncio.create_task(
                    db.log_key_usage(idx, success=False, error=err_short))

                if _is_rate_limit_error(e):
                    # Dead for now → cool it and SWITCH to the next key.
                    logger.warning("Key #%d rate-limited → switching: %s", idx, e)
                    KEY_POOL.cool(idx, GEMINI_KEY_COOLDOWN)
                    tried_keys.add(idx)
                    break  # leave inner loop → outer loop grabs next key

                if (_is_transient_error(e)
                        and transient_attempts < GEMINI_MAX_RETRIES - 1):
                    transient_attempts += 1
                    logger.warning("Transient error on key #%d (retry %d): %s",
                                   idx, transient_attempts, e)
                    await asyncio.sleep(2 * transient_attempts)
                    continue  # retry SAME key

                # Unknown / non-recoverable on this key → try the next one.
                logger.warning("Non-retryable error on key #%d → switching: %s",
                               idx, e)
                tried_keys.add(idx)
                break

    raise last_exc if last_exc else RuntimeError("All Gemini keys exhausted")


# ============================================================
# Access control
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


_last_request_at: dict[int, float] = {}


def hit_rate_limit(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    now = time.time()
    last = _last_request_at.get(user_id, 0.0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_request_at[user_id] = now
    return False


async def ensure_access(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    await db.get_or_create_user(user)  # also bumps last_seen
    uid = user.id

    if hit_rate_limit(uid):
        try:
            await update.effective_message.reply_text("⏳ براحة شوية، البوت بيعالج طلبك!")
        except Exception:
            pass
        return False

    if not _BOT_ENABLED and not is_admin(uid):
        await update.effective_message.reply_text("البوت متوقف مؤقتاً حالياً 🛑\nحاول تاني بعدين.")
        return False

    if not await db.is_allowed(uid, ADMIN_IDS):
        await update.effective_message.reply_text(
            f"معندكش صلاحية تستخدم البوت ❌\n\n"
            f"الـ ID بتاعك:\n<code>{uid}</code>\n\n"
            f"كلم الأدمن {html_escape(ADMIN_USERNAME)} وابعتله الـ ID عشان يضيفك.",
            parse_mode="HTML",
        )
        return False

    return True


async def ensure_medical_chat_access(update: Update) -> bool:
    """Require explicit admin approval when a medical feature runs in a group."""
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return True
    if await db.is_chat_enabled(chat.id):
        return True
    await update.effective_message.reply_text(
        "هذه المحادثة غير معتمدة بعد لاستخدام الأدوات الطبية.\n"
        "أرسل /register_chat إلى أدمن البوت بعد تسجيل الجروب.")
    return False


# ============================================================
# State management & timeout
# ============================================================
STATE_KEYS = (
    "pdf_text", "pdf_images", "image_data", "image_mime",
    "language", "difficulty", "awaiting_question_count",
    "admin_action", "addon_action", "_state_ts",
    "media_broadcast_target", "vp_pending",
)


def touch_state(context):
    context.user_data["_state_ts"] = time.time()


def state_is_stale(context) -> bool:
    ts = context.user_data.get("_state_ts", 0)
    return bool(ts) and (time.time() - ts > STATE_TIMEOUT)


def clear_flow_state(context):
    for k in STATE_KEYS:
        context.user_data.pop(k, None)


def clear_quiz_only(context):
    for k in ("pdf_text", "pdf_images", "image_data", "image_mime",
              "language", "difficulty", "awaiting_question_count",
              "_state_ts"):
        context.user_data.pop(k, None)


# ============================================================
# Safe message edit
# ============================================================
async def safe_edit(query, text: str, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logger.warning("safe_edit BadRequest: %s", e)
    except Exception:
        logger.exception("safe_edit unexpected failure")


# ============================================================
# Reply Keyboards
# ============================================================
def build_user_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_QUIZ_MODE), KeyboardButton(BTN_SMART_CHAT)],
        [KeyboardButton(BTN_MY_INFO),   KeyboardButton(BTN_MY_SETTINGS)],
        [KeyboardButton(BTN_HELP)],
    ]
    if not _FEATURES_KB_HIDDEN:
        keyboard[1:1] = [
            [KeyboardButton(BTN_SPOT),      KeyboardButton(BTN_QFILE)],
            [KeyboardButton(BTN_PDF_RESTYLE)],
            [KeyboardButton(BTN_VIRTUAL_PATIENT)],
            [KeyboardButton(BTN_ISLAMIC),   KeyboardButton(BTN_REFERRAL)],
            [KeyboardButton(BTN_MEDICAL)],
            [KeyboardButton(BTN_CLINICAL)],
        ]
    # Let Telegram mobile hide the keyboard when the user presses Back.
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)


def build_admin_full_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_QUIZ_MODE),   KeyboardButton(BTN_SMART_CHAT)],
        [KeyboardButton(BTN_ADMIN_PANEL), KeyboardButton(BTN_MANAGE_USERS)],
        [KeyboardButton(BTN_MANAGE_CHATS)],
        [KeyboardButton(BTN_MULTI_ADD),   KeyboardButton(BTN_SMARTDASH)],
        [KeyboardButton(BTN_TOGGLE_PDF_RESTYLE), KeyboardButton(BTN_PDF_TOKENS)],
        [KeyboardButton(BTN_PDF_TOKEN_COST)],
        [KeyboardButton(BTN_TOGGLE_VIRTUAL_PATIENT)],
        [KeyboardButton(BTN_ANALYTICS),   KeyboardButton(BTN_BROADCAST)],
        [KeyboardButton(BTN_MEDIA_BROADCAST)],
        [KeyboardButton(BTN_BACKUP),      KeyboardButton(BTN_RESTORE)],
        [KeyboardButton(BTN_BOT_STATUS),  KeyboardButton(BTN_TOGGLE_BOT)],
        [KeyboardButton(BTN_HIDE_ADMIN),
         KeyboardButton(BTN_SHOW_FEATURES if _FEATURES_KB_HIDDEN else BTN_HIDE_FEATURES)],
        [KeyboardButton(BTN_HELP)],
    ]
    if not _FEATURES_KB_HIDDEN:
        keyboard[1:1] = [
            [KeyboardButton(BTN_SPOT),        KeyboardButton(BTN_QFILE)],
            [KeyboardButton(BTN_PDF_RESTYLE)],
            [KeyboardButton(BTN_VIRTUAL_PATIENT)],
            [KeyboardButton(BTN_ISLAMIC),     KeyboardButton(BTN_MY_SETTINGS)],
            [KeyboardButton(BTN_MEDICAL)],
            [KeyboardButton(BTN_CLINICAL)],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)


def build_admin_hidden_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_QUIZ_MODE),  KeyboardButton(BTN_SMART_CHAT)],
        [KeyboardButton(BTN_SHOW_ADMIN), KeyboardButton(BTN_SHOW_FEATURES)],
        [KeyboardButton(BTN_HELP)],
    ]
    if not _FEATURES_KB_HIDDEN:
        keyboard[1:1] = [
            [KeyboardButton(BTN_SPOT),       KeyboardButton(BTN_QFILE)],
            [KeyboardButton(BTN_PDF_RESTYLE)],
            [KeyboardButton(BTN_VIRTUAL_PATIENT)],
            [KeyboardButton(BTN_ISLAMIC),    KeyboardButton(BTN_MY_SETTINGS)],
            [KeyboardButton(BTN_MEDICAL)],
            [KeyboardButton(BTN_CLINICAL)],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)


def build_smart_chat_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_CLEAR_HISTORY)],
        [KeyboardButton(BTN_EXIT_CHAT)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)


def get_keyboard_for(user_id: int) -> ReplyKeyboardMarkup:
    if is_admin(user_id):
        return build_admin_hidden_keyboard() if _ADMIN_KB_HIDDEN else build_admin_full_keyboard()
    return build_user_reply_keyboard()


# ============================================================
# User display helpers (HTML)
# ============================================================
def format_user_display_html(u: "db.User | None", fallback_id: int = 0) -> str:
    if u is None:
        return f"<code>{fallback_id}</code>"
    name = html_escape(u.first_name or "")
    username = html_escape(u.username or "")
    parts = []
    if name: parts.append(name)
    if username: parts.append(f"@{username}")
    if parts:
        return f"{' '.join(parts)} (<code>{u.id}</code>)"
    return f"<code>{u.id}</code>"


def format_quota_line(u: "db.User") -> str:
    return (f"{u.used_today}/{u.daily_limit} اليوم"
            + (f" + {u.bonus_lectures} هدية" if u.bonus_lectures else ""))


# ============================================================
# Admin inline panels
# ============================================================
async def build_admin_panel_text() -> str:
    state = "✅ شغال" if _BOT_ENABLED else "🛑 متوقف"
    total = await db.count_allowed_users()
    restyle_state = "✅ شغال" if _PDF_RESTYLE_ENABLED else "🛑 متوقف"
    virtual_state = "✅ شغال" if _VIRTUAL_PATIENT_ENABLED else "🛑 متوقف"
    return (
        f"🎛️ <b>لوحة تحكم الأدمن</b>\n\n"
        f"حالة البوت: {state}\n"
        f"عدد المستخدمين: {total}\n"
        f"إعادة تنسيق PDF بالذكاء الاصطناعي: {restyle_state}\n"
        f"تكلفة العملية: <code>{_PDF_RESTYLE_COST}</code> token\n"
        f"المريض الافتراضي: {virtual_state}\n"
        f"مفاتيح Gemini المحمّلة: {len(KEY_POOL) if KEY_POOL else 0}\n\n"
        f"استخدم الأزرار للتحكم 👇"
    )


def build_admin_keyboard_inline() -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton("🛑 إيقاف البوت", callback_data="admin:disable")
        if _BOT_ENABLED else
        InlineKeyboardButton("✅ تشغيل البوت", callback_data="admin:enable")
    )
    keyboard = [
        [toggle_btn],
        [InlineKeyboardButton(
            ("🛑 إيقاف PDF AI" if _PDF_RESTYLE_ENABLED else "✅ تشغيل PDF AI"),
            callback_data="admin:toggle_pdf_restyle")],
        [InlineKeyboardButton("🎟️ منح tokens لمستخدم", callback_data="admin:pdf_tokens_prompt")],
        [InlineKeyboardButton("💰 تغيير تكلفة العملية", callback_data="admin:pdf_cost_prompt")],
        [InlineKeyboardButton(
            ("🛑 إيقاف المريض الافتراضي" if _VIRTUAL_PATIENT_ENABLED else "✅ تشغيل المريض الافتراضي"),
            callback_data="admin:toggle_virtual_patient")],
        [InlineKeyboardButton("➕ إضافة مستخدم",     callback_data="admin:add_prompt")],
        [InlineKeyboardButton("➕👥 إضافة جماعية",   callback_data="admin:multi_add_prompt")],
        [InlineKeyboardButton("🔎 بحث عن مستخدم",    callback_data="admin:search_prompt")],
        [InlineKeyboardButton("👥 إدارة المستخدمين", callback_data="admin:list_users:0")],
        [InlineKeyboardButton("💬 المحادثات الطبية", callback_data="admin:chats")],
        [InlineKeyboardButton("📈 إحصائيات",          callback_data="admin:analytics")],
        [InlineKeyboardButton("📢 رسالة جماعية",     callback_data="admin:broadcast_prompt")],
        [InlineKeyboardButton("📸🎬 إرسال صورة/فيديو", callback_data="admin:media_broadcast_prompt")],
        [InlineKeyboardButton("🔄 تحديث",            callback_data="admin:refresh")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def _backfill_user_name(bot, u) -> None:
    """If a user row has no first_name/username, try to fetch them from
    Telegram and persist. Silent on failure (e.g. user blocked the bot or
    never started a chat with it — Telegram returns "chat not found")."""
    if u.first_name or u.username:
        return
    try:
        chat = await bot.get_chat(u.id)
    except Exception as e:
        logger.debug("backfill: get_chat(%s) failed: %s", u.id, e)
        return
    fn = (getattr(chat, "first_name", "") or "")
    ln = (getattr(chat, "last_name",  "") or "")
    un = (getattr(chat, "username",   "") or "")
    if fn or ln or un:
        await db.update_user_info_if_missing(u.id, username=un, first_name=fn, last_name=ln)
        if not u.first_name and fn: u.first_name = fn
        if not u.last_name  and ln: u.last_name  = ln
        if not u.username   and un: u.username   = un


async def build_users_list_inline(page: int = 0, bot=None) -> "tuple[str, InlineKeyboardMarkup]":
    users, total = await db.list_users_page(page, USERS_PER_PAGE)
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))

    if bot is not None:
        await asyncio.gather(*[_backfill_user_name(bot, u) for u in users],
                             return_exceptions=True)

    rows = []
    for u in users:
        name = u.first_name or u.username or "بدون اسم"
        label = f"👤 {name}"
        if u.username and u.first_name:
            label += f" (@{u.username})"
        elif not u.username and not u.first_name:
            label += f" ({u.id})"
        if len(label) > 60:
            label = label[:57] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"admin:user:{u.id}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"admin:list_users:{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="admin:refresh"))
    if page < pages - 1:
        nav_row.append(InlineKeyboardButton("التالي ➡️", callback_data=f"admin:list_users:{page+1}"))
    rows.append(nav_row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin:refresh")])

    text = f"👥 <b>المستخدمين ({total}) — صفحة {page+1}/{pages}</b>\n\nاختار واحد للتحكم فيه:"
    return text, InlineKeyboardMarkup(rows)


async def build_user_search_results(query: str, bot=None) -> "tuple[str, InlineKeyboardMarkup]":
    users = await db.search_allowed_users(query, limit=20)
    if bot is not None:
        await asyncio.gather(*[_backfill_user_name(bot, u) for u in users],
                             return_exceptions=True)
    if not users:
        return (
            f"🔎 <b>نتائج البحث</b>\n\nمفيش مستخدم مطابق لـ <code>{html_escape(query)}</code>",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔎 بحث جديد", callback_data="admin:search_prompt")
            ], [
                InlineKeyboardButton("🔙 رجوع", callback_data="admin:refresh")
            ]]),
        )
    rows = []
    for u in users:
        label = u.first_name or u.username or str(u.id)
        if u.username:
            label += f" (@{u.username})"
        rows.append([InlineKeyboardButton(
            f"👤 {label[:52]}", callback_data=f"admin:user:{u.id}")])
    rows.append([InlineKeyboardButton("🔎 بحث جديد", callback_data="admin:search_prompt")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin:refresh")])
    return (
        f"🔎 <b>نتائج البحث</b>\n\nوجدت {len(users)} مستخدم لـ <code>{html_escape(query)}</code>:",
        InlineKeyboardMarkup(rows),
    )


async def build_single_user_panel(user_id: int) -> "tuple[str, InlineKeyboardMarkup]":
    u = await db.get_user(user_id)
    if u is None:
        return ("مستخدم غير موجود", InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 رجوع", callback_data="admin:list_users:0")]]))

    tier_label = TIERS.get(u.tier, TIERS["free"])["label"]
    anon_state = "✅" if u.anonymous else "❌"
    prot_state = "✅" if u.protect_content else "❌"
    chat_state = "✅" if u.smart_chat_enabled else "❌"
    daily_str  = format_quota_line(u)

    text = (
        f"👤 <b>إدارة مستخدم</b>\n\n"
        f"الاسم: {html_escape(u.first_name or 'بدون اسم')}\n"
        f"اليوزر: " + (f"@{html_escape(u.username)}" if u.username else "—") + "\n"
        f"الـID: <code>{u.id}</code>\n"
        f"آخر نشاط: {html_escape(u.last_seen or '—')}\n"
        f"إجمالي المحاضرات: <code>{u.total_generated}</code>\n"
        f"دعوات ناجحة: <code>{u.referrals_made}</code>\n\n"
        f"📦 <b>الباقة:</b> {html_escape(tier_label)}\n"
        f"🎫 المحاضرات: <code>{html_escape(daily_str)}</code>\n"
        f"<i>(الرصيد اليومي بيتجدد كل يوم — الرصيد الإضافي مش بيتمسح)</i>\n\n"
        f"⚙️ <b>الإعدادات:</b>\n"
        f"• Anonymous: {anon_state}\n"
        f"• حماية من التحويل: {prot_state}\n"
        f"• الوضع الذكي مسموح: {chat_state}"
    )

    rows = [
        [
            InlineKeyboardButton(f"Anonymous {anon_state}", callback_data=f"admin:user:{user_id}:toggle_anon"),
            InlineKeyboardButton(f"حماية {prot_state}",     callback_data=f"admin:user:{user_id}:toggle_prot"),
        ],
        [InlineKeyboardButton(f"وضع ذكي {chat_state}", callback_data=f"admin:user:{user_id}:toggle_chat")],
        [InlineKeyboardButton("📦 تغيير الباقة",       callback_data=f"admin:user:{user_id}:tier")],
        [InlineKeyboardButton("🎁 +3 لكتشرز هدية",    callback_data=f"admin:user:{user_id}:bonus")],
        [InlineKeyboardButton("🔄 تصفير الاستهلاك",    callback_data=f"admin:user:{user_id}:reset")],
        [InlineKeyboardButton("🗑 حذف المستخدم",       callback_data=f"admin:user:{user_id}:remove")],
        [InlineKeyboardButton("🔙 رجوع للقائمة",       callback_data="admin:refresh")],
    ]
    return text, InlineKeyboardMarkup(rows)


def build_tier_picker(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for tier_key, tier_data in TIERS.items():
        rows.append([
            InlineKeyboardButton(
                f"{tier_data['label']} ({tier_data['daily_limit']} يومياً)",
                callback_data=f"admin:user:{user_id}:set_tier:{tier_key}",
            )
        ])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"admin:user:{user_id}")])
    return InlineKeyboardMarkup(rows)


# ============================================================
# Analytics panel
# ============================================================
async def build_analytics_text() -> str:
    today    = await db.stats_today()
    week     = await db.stats_window(7)
    month    = await db.stats_window(30)
    top      = await db.top_users(limit=5, days=7)
    keys     = await db.key_stats(days=1)
    total_u  = await db.count_allowed_users()

    lines = [
        "📈 <b>إحصائيات البوت</b>",
        "",
        "<b>النهارده:</b>",
        f"• محاضرات: <code>{today['generations']}</code>",
        f"• أسئلة متولّدة: <code>{today['questions']}</code>",
        f"• مستخدمين نشطين: <code>{today['active_users']}</code>",
        "",
        "<b>آخر 7 أيام:</b>",
        f"• محاضرات: <code>{week['generations']}</code> | "
        f"أسئلة: <code>{week['questions']}</code>",
        "",
        "<b>آخر 30 يوم:</b>",
        f"• محاضرات: <code>{month['generations']}</code> | "
        f"أسئلة: <code>{month['questions']}</code>",
        "",
        f"<b>إجمالي المستخدمين:</b> <code>{total_u}</code>",
        "",
    ]

    if top:
        lines.append("<b>🏆 أنشط 5 مستخدمين (آخر 7 أيام):</b>")
        for i, t in enumerate(top, 1):
            disp = html_escape(t["first_name"] or t["username"] or str(t["user_id"]))
            lines.append(f"{i}. {disp}: <code>{t['lectures']}</code> محاضرة "
                         f"({t['questions']} سؤال)")
        lines.append("")

    if keys:
        lines.append("<b>🔑 استخدام مفاتيح Gemini (آخر 24 ساعة):</b>")
        for k in keys:
            total = k["ok"] + k["fail"]
            rate  = (k["ok"] / total * 100) if total else 0
            lines.append(f"• Key #{k['key_index']}: "
                         f"<code>{k['ok']}</code>✓ / <code>{k['fail']}</code>✗ "
                         f"({rate:.0f}%)")
    else:
        lines.append("<i>مفيش استخدام لمفاتيح لسه.</i>")

    return "\n".join(lines)


def build_analytics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحديث", callback_data="admin:analytics")],
        [InlineKeyboardButton("🔙 رجوع",  callback_data="admin:refresh")],
    ])


# ============================================================
# User settings panel
# ============================================================
async def build_user_settings_panel(user_id: int) -> "tuple[str, InlineKeyboardMarkup]":
    u = await db.reset_today_if_needed(user_id)
    if u is None:
        return ("خطأ", InlineKeyboardMarkup([]))

    anon = "✅ مفعّل" if u.anonymous else "❌ متعطل"
    prot = "✅ مفعّل" if u.protect_content else "❌ متعطل"
    tier_label = TIERS.get(u.tier, TIERS["free"])["label"]
    daily_str  = format_quota_line(u)

    text = (
        f"⚙️ <b>إعداداتي</b>\n\n"
        f"📦 الباقة: {html_escape(tier_label)}\n"
        f"🎫 المحاضرات اليوم: <code>{html_escape(daily_str)}</code>\n"
        f"<i>(الرصيد اليومي بيتجدد كل يوم 🌙)</i>\n\n"
        f"🔒 <b>Anonymous Polls:</b> {anon}\n"
        f"<i>لو مفعّل، الأسئلة بتبقى مجهولة الهوية.</i>\n\n"
        f"🚫 <b>حماية من التحويل:</b> {prot}\n"
        f"<i>لو مفعّل، الأسئلة لا يمكن تحويلها لشخص تاني.</i>"
    )

    rows = [
        [InlineKeyboardButton(
            ("❌ تعطيل Anonymous" if u.anonymous else "✅ تفعيل Anonymous"),
            callback_data="settings:toggle_anon")],
        [InlineKeyboardButton(
            ("❌ تعطيل الحماية" if u.protect_content else "✅ تفعيل الحماية"),
            callback_data="settings:toggle_prot")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="settings:close")],
    ]
    return text, InlineKeyboardMarkup(rows)


async def build_my_info_text(user_id: int) -> str:
    u = await db.reset_today_if_needed(user_id)
    if u is None:
        return "حدث خطأ في جلب البيانات."
    tier_label = TIERS.get(u.tier, TIERS["free"])["label"]
    daily_str = format_quota_line(u)
    anon = "✅" if u.anonymous else "❌"
    prot = "✅" if u.protect_content else "❌"

    return (
        f"👤 <b>بياناتك</b>\n\n"
        f"الاسم: {html_escape(u.first_name or '—')}\n"
        f"اليوزر: " + (f"@{html_escape(u.username)}" if u.username else "—") + "\n"
        f"الـID: <code>{u.id}</code>\n\n"
        f"📦 الباقة: {html_escape(tier_label)}\n"
        f"🎫 المحاضرات اليوم: <code>{html_escape(daily_str)}</code>\n"
        f"📊 إجمالي محاضراتك: <code>{u.total_generated}</code>\n"
        f"🎁 دعواتك الناجحة: <code>{u.referrals_made}</code>\n\n"
        f"⚙️ Anonymous: {anon}\n"
        f"⚙️ حماية: {prot}\n\n"
        f"للاشتراك أو زيادة الباقة، كلم الأدمن {html_escape(ADMIN_USERNAME)}"
    )


async def build_referral_text(user_id: int) -> str:
    u = await db.get_user(user_id)
    refs = u.referrals_made if u else 0

    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    else:
        link = f"<i>(غير متاح — لازم الأدمن يضبط BOT_USERNAME)</i>"

    return (
        f"🎁 <b>ادعو أصحابك واكسب محاضرات إضافية!</b>\n\n"
        f"لما حد يدخل البوت من اللينك بتاعك:\n"
        f"• هو هياخد <b>{REFERRAL_REWARD}</b> محاضرة هدية 🎫\n"
        f"• وانت كمان هتاخد <b>{REFERRAL_REWARD}</b> محاضرة هدية 🎫\n\n"
        f"اللينك بتاعك:\n"
        f"<code>{link}</code>\n\n"
        f"📊 دعواتك الناجحة لحد دلوقتي: <b>{refs}</b>\n\n"
        f"<i>تقدر تشاركه في جروبات الكلية أو مع زمايلك.</i>"
    )


# ============================================================
# Multiple-Add: prompt text + processing
# ============================================================
def build_multi_add_prompt() -> str:
    """Shared prompt shown when the admin opens the bulk-add flow (from either
    the reply-keyboard button or the inline panel button)."""
    return (
        "➕👥 <b>إضافة جماعية</b>\n\n"
        "ابعت الـIDs اللي عايز تضيفها كلها مرة واحدة.\n"
        "تقدر تفصل بينهم بـ<b>مسافة</b> أو <b>فاصلة</b> أو <b>فاصلة منقوطة</b> "
        "أو <b>سطر جديد</b>.\n\n"
        "مثال:\n"
        "<code>123456789\n987654321\n555666777</code>\n\n"
        f"<i>الحد الأقصى {MAX_MULTI_ADD} ID في المرة — ابعت /cancel للإلغاء</i>"
    )


async def process_multi_add(ids: list[int]) -> "dict":
    """Add a batch of already-validated IDs in one operation.

    Detects users that are already allowed (skipped) and reports any that
    fail. Newly added users get the default settings that ``db.allow_user``
    assigns (same as the single-add flow), and Smart Mode is forced OFF.
    Returns a summary dict.
    """
    added: list[int] = []
    already: list[int] = []
    failed: list[int] = []

    for uid in ids:
        try:
            existing = await db.get_user(uid)
            if existing and existing.allowed:
                already.append(uid)
                continue
            await db.allow_user(uid)
            # v5: Smart Mode OFF by default for every newly added user.
            await db.set_setting(uid, "smart_chat_enabled", False)
            added.append(uid)
        except Exception:
            logger.exception("multi-add: failed to add %d", uid)
            failed.append(uid)

    return {"added": added, "already": already, "failed": failed}


def build_multi_add_summary(result: dict, invalid_tokens: list[str]) -> str:
    added   = result["added"]
    already = result["already"]
    failed  = result["failed"]

    lines = ["✅ <b>تمت الإضافة الجماعية</b>", ""]
    lines.append(f"➕ اتضافوا جدد: <b>{len(added)}</b>")
    lines.append(f"ℹ️ موجودين قبل كده: <b>{len(already)}</b>")
    if failed:
        lines.append(f"⚠️ فشلوا: <b>{len(failed)}</b>")
    if invalid_tokens:
        lines.append(f"❌ قيم غير صحيحة (اتجاهلت): <b>{len(invalid_tokens)}</b>")
    lines.append("")

    if added:
        preview = ", ".join(str(x) for x in added[:20])
        extra = f" …(+{len(added) - 20})" if len(added) > 20 else ""
        lines.append(f"<b>المضافين:</b> <code>{preview}{extra}</code>")
        lines.append(
            f"<i>باقة افتراضية: مجاني 🆓 ({TIERS['free']['daily_limit']} يومياً)</i>")

    if invalid_tokens:
        inv = ", ".join(html_escape(t) for t in invalid_tokens[:20])
        inv_extra = f" …(+{len(invalid_tokens) - 20})" if len(invalid_tokens) > 20 else ""
        lines.append(f"<b>تجاهلت:</b> <code>{inv}{inv_extra}</code>")

    return "\n".join(lines)


# ============================================================
# Commands
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if hit_rate_limit(user_id):
        try:
            await update.message.reply_text("⏳ براحة شوية، البوت بيعالج طلبك!")
        except Exception:
            pass
        return

    new_user = (await db.get_user(user_id)) is None
    await db.get_or_create_user(user)

    clear_flow_state(context)
    context.user_data.pop("mode", None)
    context.user_data.pop("smart_history", None)
    context.user_data.pop(LAST_MCQS_CACHE, None)

    # ── Referral payload handling: /start ref_12345 ──
    referral_msg = ""
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_") and new_user:
            try:
                referrer_id = int(arg[4:])
            except ValueError:
                referrer_id = 0
            if referrer_id and referrer_id != user_id:
                await db.allow_user(user_id)
                # v5: Smart Mode OFF by default for newly added user.
                await db.set_setting(user_id, "smart_chat_enabled", False)
                ok = await db.record_referral(referrer_id, user_id)
                if ok:
                    await db.add_bonus(referrer_id, REFERRAL_REWARD)
                    await db.add_bonus(user_id,     REFERRAL_REWARD)
                    referral_msg = (
                        f"\n\n🎉 <b>دخلت عن طريق دعوة!</b>\n"
                        f"حصلت على <b>{REFERRAL_REWARD}</b> محاضرات هدية 🎫"
                    )
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 حد جديد دخل من لينكك! حصلت على {REFERRAL_REWARD} "
                            f"محاضرات هدية 🎫"
                        )
                    except Exception:
                        pass

    if not await db.is_allowed(user_id, ADMIN_IDS):
        await update.message.reply_text(
            f"أهلاً 👋\n\n"
            f"البوت ده محدد بأشخاص معينين. عايز تستخدمه؟\n"
            f"ابعت الـID ده للأدمن {html_escape(ADMIN_USERNAME)}:\n\n"
            f"<code>{user_id}</code>",
            parse_mode="HTML",
        )
        return

    if is_admin(user_id):
        welcome = (
            "👑 <b>أهلاً بيك يا أدمن</b>\n\n"
            "اختار من الكيبورد تحت اللي عايزه:\n"
            "📝 لعمل كويز من PDF أو صورة\n"
            "🔍 تشخيص صورة (Spot Diagnosis)\n"
            "📋 كويز من ملف أسئلة جاهز\n"
            "🕌 القسم الإسلامي\n"
            "🤖 للوضع الذكي (Q&amp;A مع Gemini)\n"
            "🎛️ للوحة التحكم\n"
            "📈 للإحصائيات المتقدمة\n"
            "👥 لإدارة المستخدمين\n"
            "➕👥 لإضافة مجموعة مستخدمين دفعة واحدة\n"
            "🤖 لوحة الذكي (تشغيل/تعطيل جماعي)\n"
            "💾 نسخة احتياطية / ♻️ استعادة\n"
            "📢 لإرسال رسالة جماعية\n"
            "🙈 لإخفاء لوحة الأدمن"
        )
    else:
        u = await db.reset_today_if_needed(user_id)
        remaining = max(0, u.daily_limit - u.used_today) + u.bonus_lectures
        welcome = (
            f"أهلاً بيك 👋{referral_msg}\n\n"
            f"📦 الباقة: {html_escape(TIERS[u.tier]['label'])}\n"
            f"🎫 المحاضرات المتاحة: {remaining}\n"
            f"<i>الرصيد اليومي بيتجدد كل يوم 🌙</i>\n\n"
            f"البوت ده هيساعدك في:\n"
            f"📝 عمل كويز MCQ من PDF أو صورة لمحاضرة\n"
            f"🔍 تشخيص الصور (Spot Diagnosis)\n"
            f"📋 كويز جاهز من ملف أسئلة\n"
            f"🕌 قسم إسلامي (أذكار + قرآن + استماع)\n"
            f"🤖 الوضع الذكي: اسأل أي حاجة وهيرد عليك\n"
            f"🎁 ادعو أصحابك واكسب محاضرات إضافية!\n\n"
            f"اختار من الأزرار تحت 👇\n\n"
            f"<i>للاشتراك أو الدعم: {html_escape(ADMIN_USERNAME)}</i>"
        )

    await update.message.reply_text(
        welcome, parse_mode="HTML", reply_markup=get_keyboard_for(user_id))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.get_or_create_user(update.effective_user)
    await update.message.reply_text(
        f"الـID بتاعك:\n<code>{update.effective_user.id}</code>\n\n"
        f"كلم الأدمن {html_escape(ADMIN_USERNAME)} لو محتاج صلاحية.",
        parse_mode="HTML",
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    panel_message = await update.message.reply_text(
        await build_admin_panel_text(),
        parse_mode="HTML",
        reply_markup=build_admin_keyboard_inline(),
    )
    context.user_data["admin_panel_message_id"] = panel_message.message_id


async def register_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register the current group after its admin explicitly invites the bot."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "استخدم /register_chat داخل جروب أو سوبرجروب فقط.\n"
            "لا يستطيع البوت اكتشاف محادثاتك الخاصة تلقائيًا.")
        return
    is_group_admin = is_admin(user.id)
    if not is_group_admin:
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            is_group_admin = member.status in ("administrator", "creator")
        except Exception:
            logger.exception("Could not verify group administrator")
    if not is_group_admin:
        await update.effective_message.reply_text(
            "❌ لازم تكون أدمن في الجروب لتسجيله.")
        return
    row = await db.register_chat(
        chat_id=chat.id,
        owner_user_id=user.id,
        title=chat.title or str(chat.id),
        chat_type=chat.type,
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                "💬 <b>تم تسجيل محادثة طبية جديدة</b>\n\n"
                f"الاسم: {html_escape(row['title'])}\n"
                f"Chat ID: <code>{row['chat_id']}</code>\n"
                f"سجلها المستخدم: <code>{user.id}</code>\n\n"
                "افتح لوحة الأدمن ثم إدارة المحادثات الطبية لاعتمادها.",
                parse_mode="HTML",
            )
        except Exception:
            logger.debug("Could not notify admin %s about chat registration", admin_id,
                         exc_info=True)
    await update.effective_message.reply_text(
        "✅ تم تسجيل المحادثة الطبية وإرسالها للأدمن للمراجعة.\n"
        "الحالة الحالية: " + ("✅ معتمدة" if row["enabled"] else "⏳ في انتظار اعتماد الأدمن")
    )


async def registered_chats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await send_registered_chats(update.effective_message, context)


async def send_registered_chats(message, context):
    chats = await db.list_registered_chats()
    if not chats:
        await message.reply_text(
            "💬 لا توجد محادثات مسجلة.\n"
            "اطلب من أدمن الجروب إضافة البوت ثم إرسال /register_chat.")
        return
    rows = []
    lines = ["💬 <b>المحادثات الطبية المسجلة</b>", ""]
    for item in chats:
        state = "✅ معتمدة" if item["enabled"] else "⏳ معلقة"
        title = html_escape(item["title"] or str(item["chat_id"]))
        lines.append(f"• {title} | <code>{item['chat_id']}</code> | {state}")
        rows.append([InlineKeyboardButton(
            f"{'🛑' if item['enabled'] else '✅'} {item['title'][:35]}",
            callback_data=f"admin:chat_toggle:{item['chat_id']}")])
    rows.append([InlineKeyboardButton("🔙 لوحة الأدمن", callback_data="admin:refresh")])
    await message.reply_text("\n".join(lines), parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(rows))


async def dismiss_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove the old inline panel when the user starts another bot action."""
    message_id = context.user_data.pop("admin_panel_message_id", None)
    if message_id is None:
        return
    try:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=message_id,
        )
    except BadRequest as exc:
        logger.debug("Admin panel was already removed: %s", exc)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    had_state = bool(context.user_data.get("admin_action")
                     or context.user_data.get("addon_action")
                     or context.user_data.get("awaiting_question_count")
                     or context.user_data.get("pdf_text")
                     or context.user_data.get("pdf_images")
                     or context.user_data.get("image_data")
                     or context.user_data.get("mode") in ("smart_chat", "spot_diag",
                                                           "qfile", "quran_search",
                                                           "pdf_restyle_upload", "pdf_restyle_prompt",
                                                           "virtual_patient"))
    clear_flow_state(context)
    context.user_data.pop("mode", None)
    context.user_data.pop("smart_history", None)
    msg = "تم الإلغاء ✅" if had_state else "مفيش حاجة محتاجة إلغاء 🙂"
    await update.message.reply_text(msg, reply_markup=get_keyboard_for(user_id))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick analytics for admins (also available via inline button)."""
    if not is_admin(update.effective_user.id):
        return
    text = await build_analytics_text()
    await update.message.reply_text(text, parse_mode="HTML")


# ============================================================
# Admin inline callbacks
# ============================================================
async def on_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer("مش أدمن ❌", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "enable":
        await _set_bot_enabled(True)
        await safe_edit(query, await build_admin_panel_text(),
                        parse_mode="HTML", reply_markup=build_admin_keyboard_inline())
        return

    if action == "disable":
        await _set_bot_enabled(False)
        await safe_edit(query, await build_admin_panel_text(),
                        parse_mode="HTML", reply_markup=build_admin_keyboard_inline())
        return

    if action == "toggle_pdf_restyle":
        new_value = not _PDF_RESTYLE_ENABLED
        await _set_pdf_restyle_enabled(new_value)
        if new_value:
            import pdf_restyle
            context.user_data["mode"] = pdf_restyle.MODE_UPLOAD
            context.user_data.pop("restyle_pdf_text", None)
            context.user_data.pop("restyle_pdf_images", None)
            await safe_edit(
                query,
                "✅ PDF AI اتفعل. ابعت ملف PDF دلوقتي لإعادة تنسيقه، وبعدها اكتب وصف التصميم.",
                parse_mode="HTML",
            )
        else:
            await safe_edit(query, await build_admin_panel_text(),
                            parse_mode="HTML", reply_markup=build_admin_keyboard_inline())
        return

    if action == "toggle_virtual_patient":
        await _set_virtual_patient_enabled(not _VIRTUAL_PATIENT_ENABLED)
        await safe_edit(query, await build_admin_panel_text(),
                        parse_mode="HTML", reply_markup=build_admin_keyboard_inline())
        return

    if action == "pdf_tokens_prompt":
        context.user_data["admin_action"] = "awaiting_pdf_tokens"
        touch_state(context)
        await safe_edit(query,
                        "🎟️ ابعت: <code>user_id amount</code>\n"
                        "مثال: <code>123456789 10</code>\n"
                        "استخدم amount سالب لسحب tokens.", parse_mode="HTML")
        return

    if action == "pdf_cost_prompt":
        context.user_data["admin_action"] = "awaiting_pdf_cost"
        touch_state(context)
        await safe_edit(query,
                        f"💰 ابعت تكلفة العملية من 1 إلى 100. الحالية: {_PDF_RESTYLE_COST}")
        return

    if action == "refresh":
        await safe_edit(query, await build_admin_panel_text(),
                        parse_mode="HTML", reply_markup=build_admin_keyboard_inline())
        return

    if action == "chats":
        await send_registered_chats(query.message, context)
        return

    if action == "chat_toggle" and len(parts) >= 3:
        try:
            chat_id = int(parts[2])
        except ValueError:
            await query.answer("معرف محادثة غير صحيح", show_alert=True)
            return
        chats = await db.list_registered_chats()
        current = next((item for item in chats if item["chat_id"] == chat_id), None)
        if current is None:
            await query.answer("المحادثة غير موجودة", show_alert=True)
            return
        await db.set_chat_enabled(chat_id, not current["enabled"])
        await query.answer("تم تحديث حالة المحادثة")
        await send_registered_chats(query.message, context)
        return

    if action == "analytics":
        await safe_edit(query, await build_analytics_text(),
                        parse_mode="HTML", reply_markup=build_analytics_keyboard())
        return

    if action == "add_prompt":
        context.user_data["admin_action"] = "awaiting_add_id"
        touch_state(context)
        await safe_edit(query,
            "✏️ ابعت الـID اللي عايز تضيفه:\n<i>(أو ابعت /cancel للإلغاء)</i>",
            parse_mode="HTML")
        return

    if action == "multi_add_prompt":
        context.user_data["admin_action"] = "awaiting_multi_add_ids"
        touch_state(context)
        await safe_edit(query, build_multi_add_prompt(), parse_mode="HTML")
        return

    if action == "search_prompt":
        context.user_data["admin_action"] = "awaiting_user_search"
        touch_state(context)
        await safe_edit(
            query,
            "🔎 <b>بحث عن مستخدم</b>\n\n"
            "ابعت الـID أو اليوزر أو الاسم.\n"
            "مثال: <code>123456789</code> أو <code>ahmed</code>\n\n"
            "<i>/cancel للإلغاء</i>",
            parse_mode="HTML",
        )
        return

    if action == "broadcast_prompt":
        context.user_data["admin_action"] = "awaiting_broadcast"
        touch_state(context)
        total = await db.count_allowed_users()
        await safe_edit(query,
            f"📢 <b>رسالة جماعية</b>\n\n"
            f"اكتب الرسالة اللي عايز تبعتها لكل المستخدمين ({total}).\n\n"
            f"<i>/cancel للإلغاء</i>",
            parse_mode="HTML")
        return

    if action == "media_broadcast_prompt":
        context.user_data["admin_action"] = "awaiting_media_target"
        touch_state(context)
        await safe_edit(query,
                        "📸🎬 اكتب <code>all</code> لكل المستخدمين أو ابعت User ID واحد.",
                        parse_mode="HTML")
        return

    if action == "list_users":
        total = await db.count_allowed_users()
        if total == 0:
            await safe_edit(query,
                "📭 مفيش مستخدمين مضافين\n\nاضغط 🔙 للرجوع.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 رجوع", callback_data="admin:refresh")
                ]]))
            return
        page = 0
        if len(parts) >= 3:
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
        text, kb = await build_users_list_inline(page, bot=context.bot)
        await safe_edit(query, text, parse_mode="HTML", reply_markup=kb)
        return

    if action == "user" and len(parts) >= 3:
        try:
            uid = int(parts[2])
        except ValueError:
            return

        if len(parts) == 3:
            text, kb = await build_single_user_panel(uid)
            await safe_edit(query, text, parse_mode="HTML", reply_markup=kb)
            return

        sub = parts[3]
        if sub == "toggle_anon":
            u = await db.get_user(uid)
            if u: await db.set_setting(uid, "anonymous", not u.anonymous)
        elif sub == "toggle_prot":
            u = await db.get_user(uid)
            if u: await db.set_setting(uid, "protect_content", not u.protect_content)
        elif sub == "toggle_chat":
            u = await db.get_user(uid)
            if u: await db.set_setting(uid, "smart_chat_enabled", not u.smart_chat_enabled)
        elif sub == "tier":
            await safe_edit(query,
                f"📦 <b>اختار الباقة لـ {format_user_display_html(await db.get_user(uid), uid)}</b>",
                parse_mode="HTML",
                reply_markup=build_tier_picker(uid))
            return
        elif sub == "set_tier" and len(parts) >= 5:
            tier_key = parts[4]
            if tier_key in TIERS:
                await db.set_tier(uid, tier_key, TIERS[tier_key]["daily_limit"])
        elif sub == "reset":
            await db.reset_usage(uid)
        elif sub == "bonus":
            await db.add_bonus(uid, 3)
        elif sub == "remove":
            await db.revoke_user(uid)
            u = await db.get_user(uid)
            await safe_edit(query,
                f"🗑 تم حذف {format_user_display_html(u, uid)}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 رجوع للقائمة",
                                         callback_data="admin:list_users:0")
                ]]))
            return

        text, kb = await build_single_user_panel(uid)
        await safe_edit(query, text, parse_mode="HTML", reply_markup=kb)
        return


# ============================================================
# User settings callbacks
# ============================================================
async def on_settings_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await ensure_access(update):
        return

    user_id = query.from_user.id
    action = query.data.split(":", 1)[1]

    if action == "toggle_anon":
        u = await db.get_user(user_id)
        if u: await db.set_setting(user_id, "anonymous", not u.anonymous)
    elif action == "toggle_prot":
        u = await db.get_user(user_id)
        if u: await db.set_setting(user_id, "protect_content", not u.protect_content)
    elif action == "close":
        await safe_edit(query, "تم ✅")
        return

    text, kb = await build_user_settings_panel(user_id)
    await safe_edit(query, text, parse_mode="HTML", reply_markup=kb)


# ============================================================
# Document handler
# ============================================================
async def handle_admin_media_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if context.user_data.get("admin_action") != "awaiting_media_upload":
        return

    message = update.effective_message
    media_type = ""
    file_id = ""
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
    elif message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("image/"):
            media_type = "photo"
            file_id = message.document.file_id

    if not file_id:
        await message.reply_text("❌ ابعت صورة أو فيديو فقط.")
        raise ApplicationHandlerStop

    target = context.user_data.pop("media_broadcast_target", "")
    context.user_data.pop("admin_action", None)
    caption = message.caption or ""
    addons.track(
        "media_broadcast",
        do_media_broadcast(context, update.effective_user.id, target,
                           media_type, file_id, caption),
    )
    await message.reply_text("📤 بدأ إرسال الوسائط في الخلفية…")
    raise ApplicationHandlerStop


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") == "pdf_restyle_upload":
        import pdf_restyle
        await pdf_restyle._intercept_document(update, context)
        return

    if not await ensure_access(update):
        return

    user_id = update.effective_user.id
    context.user_data.pop("mode", None)

    doc = update.message.document
    mime  = (doc.mime_type or "").lower()
    fname = (doc.file_name or "").lower()

    is_pdf   = mime == "application/pdf" or fname.endswith(".pdf")
    is_image = (mime.startswith("image/")
                or any(fname.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")))

    if not (is_pdf or is_image):
        await update.message.reply_text(
            "الملف لازم يكون PDF أو صورة (JPG/PNG/WEBP) ❌",
            reply_markup=get_keyboard_for(user_id))
        return

    file_size = doc.file_size or 0
    if file_size and file_size > MAX_FILE_SIZE:
        mb = file_size / (1024 * 1024)
        await update.message.reply_text(
            f"❌ الملف كبير جداً ({mb:.1f} ميجا)\n"
            f"الحد الأقصى: 20 ميجا.")
        return

    can_proceed, daily_rem, bonus_rem, daily_limit = await db.can_consume(user_id)
    if not can_proceed and not is_admin(user_id):
        await update.message.reply_text(
            f"❌ <b>خلصت محاضراتك النهارده</b>\n\n"
            f"حدك اليومي: <code>{daily_limit}</code>\n"
            f"المتبقي: <code>0</code>\n\n"
            f"الرصيد بيتجدد تلقائياً بعد منتصف الليل 🌙\n"
            f"أو ادعو أصحابك من زرار 🎁 لتحصل على محاضرات هدية!\n"
            f"لزيادة الباقة كلم الأدمن {html_escape(ADMIN_USERNAME)}",
            parse_mode="HTML",
            reply_markup=get_keyboard_for(user_id))
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    status = await update.message.reply_text("جاري تحليل الملف… ⏳")

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
    except Exception:
        logger.exception("File download failed")
        await status.edit_text("حصل خطأ في تحميل الملف ❌")
        return

    clear_quiz_only(context)

    if is_pdf:
        try:
            text, images = await asyncio.to_thread(
                extract_pdf_text_and_images, bytes(file_bytes))
        except PdfTooLargeError as e:
            await status.edit_text(
                f"❌ ملف PDF كبير: {e}\nالحد الأقصى: {MAX_PDF_PAGES} صفحة.")
            return
        except Exception:
            logger.exception("PDF extraction failed")
            await status.edit_text("حصل خطأ في قراءة الملف ❌")
            return

        if (not text or len(text.strip()) < 200) and not images:
            await status.edit_text(
                "معرفتش أستخرج محتوى كافي من الـPDF ❌\n"
                "لو الملف ده صور سكان، ابعتهم كصور مباشرة.")
            return

        if text and len(text) > MAX_PDF_CHARS:
            text = text[:MAX_PDF_CHARS]

        context.user_data["pdf_text"]   = text or ""
        context.user_data["pdf_images"] = images
        info_line = f"\n🖼 وكمان لقيت {len(images)} صورة جوه الـPDF، هحللها برضو." if images else ""
        await status.edit_text(f"تم تحليل الملف بنجاح ✅{info_line}")
        context.user_data["_source_type"] = "pdf"
    else:
        context.user_data["image_data"] = bytes(file_bytes)
        context.user_data["image_mime"] = mime or "image/jpeg"
        await status.edit_text("تم تحليل الملف بنجاح ✅")
        context.user_data["_source_type"] = "image"

    await db.consume(user_id) if not is_admin(user_id) else None
    touch_state(context)
    await ask_language(update.message)


# ============================================================
# Photo handler
# ============================================================
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_access(update):
        return

    user_id = update.effective_user.id
    context.user_data.pop("mode", None)

    photos = update.message.photo
    if not photos:
        return

    photo = photos[-1]
    if photo.file_size and photo.file_size > MAX_FILE_SIZE:
        mb = photo.file_size / (1024 * 1024)
        await update.message.reply_text(
            f"❌ الصورة كبيرة جداً ({mb:.1f} ميجا)\nالحد الأقصى: 20 ميجا.")
        return

    can_proceed, _, _, daily_limit = await db.can_consume(user_id)
    if not can_proceed and not is_admin(user_id):
        await update.message.reply_text(
            f"❌ خلصت محاضراتك النهارده ({daily_limit}/يوم).\n"
            f"ادعو أصحابك من زرار 🎁 لتاخد محاضرات هدية!",
            reply_markup=get_keyboard_for(user_id))
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    status = await update.message.reply_text("جاري تحليل الصورة… ⏳")

    try:
        tg_file = await photo.get_file()
        img_bytes = await tg_file.download_as_bytearray()
    except Exception:
        logger.exception("Photo download failed")
        await status.edit_text("حصل خطأ في تحميل الصورة ❌")
        return

    clear_quiz_only(context)
    context.user_data["image_data"] = bytes(img_bytes)
    context.user_data["image_mime"] = "image/jpeg"
    context.user_data["_source_type"] = "image"

    if not is_admin(user_id):
        await db.consume(user_id)
    touch_state(context)
    await status.edit_text("تم استلام الصورة ✅")
    await ask_language(update.message)


async def ask_language(message):
    keyboard = [
        [InlineKeyboardButton(LANG_LABELS["auto"], callback_data="lang:auto")],
        [InlineKeyboardButton(LANG_LABELS["ar"],   callback_data="lang:ar")],
        [InlineKeyboardButton(LANG_LABELS["en"],   callback_data="lang:en")],
        [InlineKeyboardButton(BTN_CANCEL,          callback_data="cancel:flow")],
    ]
    await message.reply_text("اختار لغة الأسئلة 🗣️",
                             reply_markup=InlineKeyboardMarkup(keyboard))


async def on_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await ensure_access(update):
        return

    if state_is_stale(context):
        clear_flow_state(context)
        await safe_edit(query, "الجلسة انتهت ⏰ ابعت الملف تاني.")
        return

    have_content = (context.user_data.get("pdf_text") or
                    context.user_data.get("pdf_images") or
                    context.user_data.get("image_data"))
    if not have_content:
        await safe_edit(query, "ابعت ملف PDF أو صورة الأول 📄")
        return

    lang = query.data.split(":", 1)[1]
    context.user_data["language"] = lang
    touch_state(context)

    keyboard = [
        [InlineKeyboardButton(DIFFICULTY_LABELS["easy"],   callback_data="diff:easy")],
        [InlineKeyboardButton(DIFFICULTY_LABELS["medium"], callback_data="diff:medium")],
        [InlineKeyboardButton(DIFFICULTY_LABELS["hard"],   callback_data="diff:hard")],
        [InlineKeyboardButton(DIFFICULTY_LABELS["mixed"],  callback_data="diff:mixed")],
        [InlineKeyboardButton(BTN_CANCEL,                  callback_data="cancel:flow")],
    ]
    await safe_edit(query,
        f"اللغة: {LANG_LABELS[lang]}\n\nاختار درجة الصعوبة 🎯",
        reply_markup=InlineKeyboardMarkup(keyboard))


async def on_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await ensure_access(update):
        return

    if state_is_stale(context):
        clear_flow_state(context)
        await safe_edit(query, "الجلسة انتهت ⏰ ابعت الملف تاني.")
        return

    have_content = (context.user_data.get("pdf_text") or
                    context.user_data.get("pdf_images") or
                    context.user_data.get("image_data"))
    if not have_content:
        await safe_edit(query, "ابعت ملف الأول 📄")
        return

    diff = query.data.split(":", 1)[1]
    context.user_data["difficulty"] = diff
    context.user_data["awaiting_question_count"] = True
    touch_state(context)

    lang = context.user_data.get("language", "auto")
    await safe_edit(query,
        f"اللغة: {LANG_LABELS[lang]}\n"
        f"الصعوبة: {DIFFICULTY_LABELS[diff]}\n\n"
        f"📝 <b>عايز كام سؤال؟</b>\n"
        f"اكتب رقم من {MIN_QUESTIONS} لـ {MAX_QUESTIONS}\n\n"
        f"<i>/cancel للإلغاء</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(BTN_CANCEL, callback_data="cancel:flow")
        ]]))


async def on_cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("تم الإلغاء")
    clear_flow_state(context)
    await safe_edit(query, "تم إلغاء العملية ✅")


# ============================================================
# MCQ generation entry point
# ============================================================
async def generate_mcqs_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 n_questions: int):
    user_id    = update.effective_user.id
    text       = context.user_data.get("pdf_text")
    pdf_images = context.user_data.get("pdf_images") or []
    image_data = context.user_data.get("image_data")
    image_mime = context.user_data.get("image_mime")
    source     = context.user_data.get("_source_type", "")

    if not (text or pdf_images or image_data):
        await update.message.reply_text("ابعت ملف الأول 📄")
        return

    language   = context.user_data.get("language", "auto")
    difficulty = context.user_data.get("difficulty", "mixed")

    await update.message.reply_text("استعنا على الشقي بالله 💪")
    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        # v5: single batched entry point. Splits big sets (100/200+) into
        # chunks so the JSON never truncates, runs a few in parallel.
        mcqs = await addons.generate_mcqs_smart(
            text=text, pdf_images=pdf_images,
            image_data=image_data, image_mime=image_mime,
            n=n_questions, difficulty=difficulty, language=language)
    except Exception as e:
        logger.exception("MCQ generation failed")
        await update.message.reply_text(
            f"حصل خطأ في توليد الأسئلة ❌\n<code>{html_escape(type(e).__name__)}: "
            f"{html_escape(str(e))}</code>",
            parse_mode="HTML")
        await db.log_generation(user_id, n_questions, source, language, difficulty, False)
        clear_quiz_only(context)
        return

    if not mcqs:
        await update.message.reply_text(
            "معرفتش أولّد أسئلة من المحتوى ده ❌\nجرّب تاني أو ابعت ملف تاني.")
        await db.log_generation(user_id, n_questions, source, language, difficulty, False)
        clear_quiz_only(context)
        return

    await send_quizzes(update.message.chat, mcqs, user_id)
    await db.log_generation(user_id, len(mcqs), source, language, difficulty, True)

    context.user_data[LAST_MCQS_CACHE] = mcqs

    export_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 تحميل PDF",    callback_data="export:pdf"),
         InlineKeyboardButton("🃏 تحميل Anki",   callback_data="export:anki")],
    ])
    await update.message.reply_text(
        "عايز تحفظ الأسئلة للمراجعة؟ اختار صيغة 👇",
        reply_markup=export_kb,
    )

    clear_quiz_only(context)


# ============================================================
# Export handler
# ============================================================
async def on_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await ensure_access(update):
        return

    mcqs = context.user_data.get(LAST_MCQS_CACHE)
    if not mcqs:
        await query.answer("الأسئلة مش متاحة دلوقتي، ولّد كويز جديد", show_alert=True)
        return

    fmt = query.data.split(":", 1)[1]
    chat = query.message.chat

    await chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    status = await chat.send_message("جاري تجهيز الملف… ⏳")

    try:
        if fmt == "pdf":
            import qexport
            data = await asyncio.to_thread(
                qexport.build_quiz_pdf, mcqs,
                title="أسئلة الاختيار من متعدد", with_answers=True)
            filename = "quiz.pdf"
        elif fmt == "anki":
            data = await asyncio.to_thread(exporter.generate_anki, mcqs, "MCQ Deck")
            filename = "quiz.apkg"
        else:
            await status.edit_text("صيغة غير معروفة ❌")
            return
    except Exception as e:
        logger.exception("Export failed")
        await status.edit_text(
            f"حصل خطأ في التصدير ❌\n<code>{html_escape(type(e).__name__)}</code>",
            parse_mode="HTML")
        return

    try:
        await chat.send_document(
            document=InputFile(BytesIO(data), filename=filename),
            caption=("📄 ملف PDF فيه كل الأسئلة + صفحة الإجابات"
                     if fmt == "pdf" else
                     "🃏 ديك Anki جاهز للاستيراد")
        )
        await status.delete()
    except Exception as e:
        logger.exception("Send document failed")
        await status.edit_text(f"حصل خطأ في إرسال الملف: {html_escape(str(e))}",
                               parse_mode="HTML")


# ============================================================
# Smart Chat Mode
# ============================================================
async def enter_smart_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u = await db.get_user(user_id)

    if u and not u.smart_chat_enabled:
        await update.message.reply_text(
            f"الوضع الذكي متعطل لحسابك ❌\nكلم الأدمن {ADMIN_USERNAME}.",
            reply_markup=get_keyboard_for(user_id))
        return

    context.user_data["mode"] = "smart_chat"
    context.user_data.setdefault("smart_history", [])
    await update.message.reply_text(
        "🤖 <b>دخلت الوضع الذكي</b>\n\n"
        "اسأل أي حاجة وأنا هرد عليك بمساعدة Gemini.\n"
        "للخروج اضغط 🚪 أو ابعت /start",
        parse_mode="HTML", reply_markup=build_smart_chat_keyboard())


async def exit_smart_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.pop("mode", None)
    context.user_data.pop("smart_history", None)
    await update.message.reply_text("خرجت من الوضع الذكي 👋",
                                    reply_markup=get_keyboard_for(user_id))


async def handle_smart_chat(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            user_text: str):
    user_id = update.effective_user.id
    await update.message.chat.send_action(ChatAction.TYPING)

    history = context.user_data.get("smart_history", [])
    history.append({"role": "user", "text": user_text})
    if len(history) > 20:
        history = history[-20:]

    contents = []
    for msg_item in history:
        contents.append({
            "role": "user" if msg_item["role"] == "user" else "model",
            "parts": [{"text": msg_item["text"]}],
        })

    try:
        response = await call_gemini(
            model=GEMINI_MODEL, contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.7, max_output_tokens=2000,
                system_instruction=(
                    "You are a helpful, knowledgeable assistant. "
                    "Reply in the same language as the user's last message. "
                    "Be concise but thorough. Use Markdown when helpful."
                ),
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        reply_text = (response.text or "").strip()
    except Exception as e:
        logger.exception("Smart chat failed")
        await update.message.reply_text(
            f"حصل خطأ ❌\n<code>{html_escape(type(e).__name__)}: "
            f"{html_escape(str(e))}</code>", parse_mode="HTML")
        return

    if not reply_text:
        await update.message.reply_text("معرفتش أرد دلوقتي، جرب تاني 🤔")
        return

    if len(reply_text) > MAX_SMART_REPLY:
        reply_text = reply_text[:MAX_SMART_REPLY] + "\n…(تم اختصار الرد)"

    history.append({"role": "model", "text": reply_text})
    context.user_data["smart_history"] = history

    u = await db.get_user(user_id)
    protect = bool(u and u.protect_content)
    try:
        await update.message.reply_text(reply_text, parse_mode="Markdown",
                                        protect_content=protect)
    except Exception:
        await update.message.reply_text(reply_text, protect_content=protect)


# ============================================================
# Broadcast
# ============================================================
async def do_broadcast(context: ContextTypes.DEFAULT_TYPE, sender_id: int,
                       message_text: str):
    targets = await db.list_allowed_user_ids()
    sent = 0
    failed = 0
    blocked = 0

    progress_msg = None
    try:
        progress_msg = await context.bot.send_message(
            sender_id, f"📢 جاري الإرسال لـ {len(targets)} مستخدم…")
    except Exception:
        pass


    header = "📢 <b>رسالة من الإدارة:</b>\n\n"
    for i, uid in enumerate(targets, 1):
        if uid == sender_id:
            continue
        try:
            await context.bot.send_message(uid, header + message_text,
                                           parse_mode="HTML")
            sent += 1
        except BadRequest:
            try:
                await context.bot.send_message(uid, "📢 رسالة من الإدارة:\n\n" + message_text)
                sent += 1
            except Exception:
                failed += 1
        except Forbidden:
            blocked += 1
        except Exception as e:
            logger.warning("Broadcast to %d failed: %s", uid, e)
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)

        if progress_msg and i % 20 == 0:
            try:
                await progress_msg.edit_text(f"📢 جاري الإرسال… {i}/{len(targets)}")
            except Exception:
                pass

    summary = (
        f"✅ <b>تمت الرسالة الجماعية</b>\n\n"
        f"تم: {sent}\n"
        f"محظور البوت: {blocked}\n"
        f"فشل: {failed}\n"
        f"إجمالي: {len(targets)}"
    )
    try:
        if progress_msg:
            await progress_msg.edit_text(summary, parse_mode="HTML")
        else:
            await context.bot.send_message(sender_id, summary, parse_mode="HTML")
    except Exception:
        pass


async def do_media_broadcast(context: ContextTypes.DEFAULT_TYPE, sender_id: int,
                             target: str, media_type: str, file_id: str,
                             caption: str = ""):
    targets = (await db.list_allowed_user_ids()
               if target == "all" else [int(target)])
    sent = failed = blocked = 0
    for uid in targets:
        if target == "all" and uid == sender_id:
            continue
        try:
            if media_type == "photo":
                await context.bot.send_photo(uid, photo=file_id, caption=caption or None)
            else:
                await context.bot.send_video(uid, video=file_id, caption=caption or None)
            sent += 1
        except Forbidden:
            blocked += 1
        except Exception as exc:
            logger.warning("Media broadcast to %d failed: %s", uid, exc)
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)

    summary = (f"✅ <b>تم إرسال الوسائط</b>\n\n"
               f"المستهدف: {'كل المستخدمين' if target == 'all' else target}\n"
               f"تم: {sent}\nمحظور البوت: {blocked}\nفشل: {failed}")
    try:
        await context.bot.send_message(sender_id, summary, parse_mode="HTML")
    except Exception:
        logger.debug("Could not send media broadcast summary", exc_info=True)


# ============================================================
# Text handler
# ============================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.get_or_create_user(update.effective_user)
    user_id = update.effective_user.id
    text    = (update.message.text or "").strip()
    await dismiss_admin_panel(update, context)

    if state_is_stale(context):
        clear_flow_state(context)

    # Handle the numeric question-count reply before the general menu fallback.
    if context.user_data.get("awaiting_question_count"):
        if not text.isdecimal():
            await update.message.reply_text(
                f"❌ اكتب عددًا صحيحًا من {MIN_QUESTIONS} إلى {MAX_QUESTIONS}.\n"
                "أو أرسل /cancel للإلغاء."
            )
            return

        n_questions = int(text)
        if not MIN_QUESTIONS <= n_questions <= MAX_QUESTIONS:
            await update.message.reply_text(
                f"❌ العدد لازم يكون من {MIN_QUESTIONS} إلى {MAX_QUESTIONS}. "
                "جرّب رقمًا تانيًا أو أرسل /cancel للإلغاء."
            )
            return

        have_content = (context.user_data.get("pdf_text") or
                        context.user_data.get("pdf_images") or
                        context.user_data.get("image_data"))
        if not have_content:
            clear_quiz_only(context)
            await update.message.reply_text("انتهت بيانات الملف. ابعت PDF أو صورة من جديد 📄")
            return

        touch_state(context)
        await generate_mcqs_and_send(update, context, n_questions)
        return

    if is_admin(user_id) and text == BTN_TOGGLE_VIRTUAL_PATIENT:
        await _set_virtual_patient_enabled(not _VIRTUAL_PATIENT_ENABLED)
        state = "✅ شغال" if _VIRTUAL_PATIENT_ENABLED else "🛑 متوقف"
        await update.message.reply_text(
            f"🩺 المريض الافتراضي: {state}",
            reply_markup=get_keyboard_for(user_id))
        return

    # ── v5: Quran search-by-text (islamic module) ──
    if await islamic.handle_search_text(update, context, text):
        return

    # ── Admin: search users by ID, username, or name ──
    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_user_search":
        context.user_data.pop("admin_action", None)
        if not text:
            await update.message.reply_text("❌ اكتب ID أو اسم أو يوزر للبحث.")
            return
        result_text, result_kb = await build_user_search_results(text, bot=context.bot)
        await update.message.reply_text(result_text, parse_mode="HTML",
                                        reply_markup=result_kb)
        return

    # ── Admin: awaiting a user ID to add ──
    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_add_id":
        context.user_data.pop("admin_action", None)
        try:
            new_id = int(text)
        except ValueError:
            await update.message.reply_text(
                "❌ الـID لازم يكون رقم، حاول تاني.",
                reply_markup=get_keyboard_for(user_id))
            return

        existing = await db.get_user(new_id)
        if existing and existing.allowed:
            await update.message.reply_text(
                f"ℹ️ المستخدم <code>{new_id}</code> مضاف بالفعل من قبل.",
                parse_mode="HTML", reply_markup=get_keyboard_for(user_id))
            return

        await db.allow_user(new_id)
        # v5: Smart Mode OFF by default for newly added user.
        await db.set_setting(new_id, "smart_chat_enabled", False)
        await update.message.reply_text(
            f"✅ تمت إضافة <code>{new_id}</code>\n"
            f"باقة افتراضية: مجاني 🆓 ({TIERS['free']['daily_limit']} يومياً)",
            parse_mode="HTML", reply_markup=get_keyboard_for(user_id))
        return

    # ── Admin: awaiting MULTIPLE IDs to add (bulk / "Multiple Add") ──
    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_multi_add_ids":
        context.user_data.pop("admin_action", None)

        valid_ids, invalid_tokens = parse_id_list(text)

        # Nothing usable at all in the message.
        if not valid_ids and not invalid_tokens:
            await update.message.reply_text(
                "❌ مفيش أي IDs في الرسالة. حاول تاني أو ابعت /cancel.",
                reply_markup=get_keyboard_for(user_id))
            return

        # Only garbage tokens, no valid IDs.
        if not valid_ids:
            inv = ", ".join(html_escape(t) for t in invalid_tokens[:30])
            await update.message.reply_text(
                "❌ مفيش ولا ID صحيح. كل ID لازم يكون رقم موجب.\n"
                f"القيم غير الصحيحة: <code>{inv}</code>",
                parse_mode="HTML", reply_markup=get_keyboard_for(user_id))
            return

        # Too many at once → reject the whole batch (clear, predictable).
        if len(valid_ids) > MAX_MULTI_ADD:
            await update.message.reply_text(
                f"❌ عدد الـIDs كبير ({len(valid_ids)}).\n"
                f"الحد الأقصى {MAX_MULTI_ADD} في المرة الواحدة — قسّمهم على دفعات.",
                reply_markup=get_keyboard_for(user_id))
            return

        # All entered data validated → save in one operation, then report.
        result = await process_multi_add(valid_ids)
        await update.message.reply_text(
            build_multi_add_summary(result, invalid_tokens),
            parse_mode="HTML", reply_markup=get_keyboard_for(user_id))
        return

    # ── Admin: awaiting broadcast body ──
    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_broadcast":
        context.user_data.pop("admin_action", None)
        if not text:
            await update.message.reply_text("❌ الرسالة فاضية.")
            return
        addons.track("broadcast", do_broadcast(context, user_id, text))
        await update.message.reply_text("📢 بدأت إرسال الرسالة في الخلفية…",
                                        reply_markup=get_keyboard_for(user_id))
        return

    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_media_target":
        target_text = text.strip().lower()
        if target_text == "all":
            target = "all"
        else:
            try:
                target_id = int(target_text)
                if target_id <= 0 or not await db.is_allowed(target_id, ADMIN_IDS):
                    raise ValueError
            except (ValueError, TypeError):
                await update.message.reply_text(
                    "❌ اكتب all أو User ID لمستخدم مسموح له.")
                return
            target = str(target_id)
        context.user_data["admin_action"] = "awaiting_media_upload"
        context.user_data["media_broadcast_target"] = target
        touch_state(context)
        await update.message.reply_text(
            f"✅ المستهدف: {'كل المستخدمين' if target == 'all' else target}\n"
            "ابعت صورة أو فيديو الآن. يمكن أن تضع الوصف داخل Caption.")
        return

    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_pdf_tokens":
        context.user_data.pop("admin_action", None)
        parts = text.split()
        try:
            target_id, amount = int(parts[0]), int(parts[1])
            if len(parts) != 2 or target_id <= 0 or amount == 0:
                raise ValueError
        except (ValueError, IndexError):
            await update.message.reply_text("❌ الصيغة الصحيحة: user_id amount مثل 123456789 10")
            return
        balance = await db.add_pdf_tokens(target_id, amount)
        await update.message.reply_text(
            f"✅ رصيد <code>{target_id}</code>: <b>{balance}</b> token",
            parse_mode="HTML", reply_markup=get_keyboard_for(user_id))
        return

    if is_admin(user_id) and context.user_data.get("admin_action") == "awaiting_pdf_cost":
        context.user_data.pop("admin_action", None)
        try:
            cost = int(text)
            if not 1 <= cost <= 100:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ التكلفة لازم تكون رقم من 1 إلى 100.")
            return
        await _set_pdf_restyle_cost(cost)
        await update.message.reply_text(f"✅ تكلفة إعادة تنسيق PDF أصبحت {cost} token.",
                                        reply_markup=get_keyboard_for(user_id))
        return

    if context.user_data.get("mode") == "pdf_restyle_prompt":
        import pdf_restyle
        await pdf_restyle.handle_prompt(update, context, text)
        return

    if context.user_data.get("mode") == "virtual_patient":
        import virtual_patient
        if await virtual_patient.handle_text(update, context, text):
            return

    # ── Shared buttons ──
    if text == BTN_ISLAMIC:
        await islamic.cmd_islamic(update, context)
        return

    if text == BTN_SPOT:
        await addons.cmd_spot(update, context)
        return

    if text == BTN_QFILE:
        await addons._cmd_qfile(update, context)
        return

    if text == BTN_PDF_RESTYLE:
        import pdf_restyle
        await pdf_restyle.cmd_restyle(update, context)
        return

    if text == BTN_MEDICAL:
        import medical
        await medical.cmd_diagnose(update, context)
        return

    if text == BTN_CLINICAL:
        import medical
        await medical.cmd_clinical(update, context)
        return

    if text == BTN_VIRTUAL_PATIENT:
        import virtual_patient
        await virtual_patient.cmd_virtual_patient(update, context)
        return

    if text == BTN_HELP:
        if is_admin(user_id):
            help_text = (
                "ℹ️ <b>المساعدة - وضع الأدمن</b>\n\n"
                "• 📝 <b>كويز</b>: PDF/صورة → لغة → صعوبة → عدد الأسئلة\n"
                "• 🔍 <b>تشخيص صورة</b>: ابعت صورة وهتيجي الإجابة مخفية\n"
                "• 📋 <b>كويز من ملف</b>: ملف أسئلة جاهز → كويز\n"
                "• 🕌 <b>إسلامي</b>: أذكار + قرآن + استماع\n"
                "• 🤖 <b>الوضع الذكي</b>: Q&amp;A مع Gemini\n"
                "• 🎁 <b>ادعو أصحابك</b>: لينك إحالة + محاضرات هدية\n\n"
                "💡 بعد كل كويز ممكن تنزّله PDF أو Anki deck.\n\n"
                "أوامر:\n"
                "/start - إعادة التشغيل\n"
                "/admin - لوحة التحكم\n"
                "/stats - الإحصائيات\n"
                "/smartdash - لوحة الوضع الذكي\n"
                "/backup /restore - نسخ واستعادة\n"
                "/health /tasks - حالة النظام والمهام\n"
                "/spot - تشخيص صورة\n"
                "/clinical - حالات إكلينيكية وتدريب على القرار الطبي\n"
                "/qfile - كويز من ملف\n"
                "/islamic - القسم الإسلامي\n"
                "/register_chat - تسجيل الجروب الطبي بعد إضافة البوت\n"
                "/medical_chats - إدارة المحادثات الطبية\n"
                "/cancel - إلغاء أي عملية\n"
                "/myid - الـID بتاعك"
            )
        else:
            help_text = (
                "ℹ️ <b>المساعدة</b>\n\n"
                "• 📝 <b>كويز</b>: ابعت PDF أو صورة، اختار اللغة والصعوبة\n"
                "• 🔍 <b>تشخيص صورة</b>: ابعت صورة والإجابة هتكون مخفية تكشفها بضغطة\n"
                "• 📋 <b>كويز من ملف</b>: ابعت ملف أسئلة جاهز يتحوّل كويز\n"
                "• 🕌 <b>إسلامي</b>: أذكار + قرآن كامل + استماع بقرّاء مختلفين\n"
                "• 🤖 <b>الوضع الذكي</b>: اسأل أي حاجة\n"
                "• 🎁 <b>ادعو أصحابك</b>: لينك إحالة + محاضرات هدية\n\n"
                "💡 بعد كل كويز ممكن تنزّله PDF أو Anki deck.\n\n"
                "أوامر:\n"
                "/spot - تشخيص صورة\n"
                "/clinical - حالات إكلينيكية وتدريب على القرار الطبي\n"
                "/qfile - كويز من ملف\n"
                "/islamic - القسم الإسلامي\n"
                "/register_chat - تسجيل جروب طبي بعد إضافة البوت (بواسطة أدمن الجروب)\n"
                "/cancel - إلغاء أي عملية\n"
                "/myid - الـID بتاعك\n\n"
                f"للاشتراك أو الدعم: {html_escape(ADMIN_USERNAME)}"
            )
        await update.message.reply_text(help_text, parse_mode="HTML")
        return

    if text == BTN_MY_INFO:
        if not await ensure_access(update):
            return
        await update.message.reply_text(await build_my_info_text(user_id),
                                        parse_mode="HTML")
        return

    if text == BTN_MY_SETTINGS:
        if not await ensure_access(update):
            return
        panel_text, kb = await build_user_settings_panel(user_id)
        await update.message.reply_text(panel_text, parse_mode="HTML", reply_markup=kb)
        return

    if text == BTN_REFERRAL:
        if not await ensure_access(update):
            return
        await update.message.reply_text(await build_referral_text(user_id),
                                        parse_mode="HTML",
                                        disable_web_page_preview=True)
        return

    if text == BTN_QUIZ_MODE:
        if not await ensure_access(update):
            return
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            "📝 <b>وضع الكويز</b>\n\n"
            "ابعتلي ملف PDF أو صورة لمحاضرة 📄🖼\n"
            "وأنا هحوّلها لأسئلة MCQ تفاعلية 🎯\n\n"
            "<i>الحد الأقصى: 20 ميجا، 100 صفحة للـPDF.</i>",
            parse_mode="HTML", reply_markup=get_keyboard_for(user_id))
        return

    if text == BTN_SMART_CHAT:
        if not await ensure_access(update):
            return
        await enter_smart_chat(update, context)
        return

    if text == BTN_EXIT_CHAT:
        await exit_smart_chat(update, context)
        return

    if text == BTN_CLEAR_HISTORY:
        if context.user_data.get("mode") == "smart_chat":
            context.user_data["smart_history"] = []
            await update.message.reply_text("🧹 تم مسح المحادثة.")
        else:
            await update.message.reply_text("مفيش محادثة شغالة حالياً.")
        return

    if text == BTN_START_OVER:
        await start(update, context)
        return

    # ── Admin buttons ──
    if is_admin(user_id):
        if text == BTN_TOGGLE_VIRTUAL_PATIENT:
            await _set_virtual_patient_enabled(not _VIRTUAL_PATIENT_ENABLED)
            state = "✅ شغال" if _VIRTUAL_PATIENT_ENABLED else "🛑 متوقف"
            await update.message.reply_text(
                f"🩺 المريض الافتراضي: {state}",
                reply_markup=get_keyboard_for(user_id))
            return

        if text == BTN_TOGGLE_PDF_RESTYLE:
            await _set_pdf_restyle_enabled(not _PDF_RESTYLE_ENABLED)
            state = "✅ شغال" if _PDF_RESTYLE_ENABLED else "🛑 متوقف"
            if _PDF_RESTYLE_ENABLED:
                import pdf_restyle
                context.user_data["mode"] = pdf_restyle.MODE_UPLOAD
                context.user_data.pop("restyle_pdf_text", None)
                context.user_data.pop("restyle_pdf_images", None)
                await update.message.reply_text(
                    "✅ PDF AI اتفعل. ابعت ملف PDF دلوقتي لإعادة تنسيقه، وبعدها اكتب وصف التصميم.",
                    reply_markup=get_keyboard_for(user_id))
            else:
                await update.message.reply_text(
                    f"إعادة تنسيق PDF بالذكاء الاصطناعي: {state}",
                    reply_markup=get_keyboard_for(user_id))
            return

        if text == BTN_PDF_TOKENS:
            context.user_data["admin_action"] = "awaiting_pdf_tokens"
            touch_state(context)
            await update.message.reply_text(
                "🎟️ ابعت: <code>user_id amount</code>\nمثال: <code>123456789 10</code>",
                parse_mode="HTML")
            return

        if text == BTN_PDF_TOKEN_COST:
            context.user_data["admin_action"] = "awaiting_pdf_cost"
            touch_state(context)
            await update.message.reply_text(
                f"💰 ابعت تكلفة العملية من 1 إلى 100. الحالية: {_PDF_RESTYLE_COST}")
            return

        if text == BTN_MULTI_ADD:
            context.user_data["admin_action"] = "awaiting_multi_add_ids"
            touch_state(context)
            await update.message.reply_text(
                build_multi_add_prompt(), parse_mode="HTML")
            return

        if text == BTN_BACKUP:
            await addons.cmd_backup(update, context)
            return

        if text == BTN_RESTORE:
            await addons.cmd_restore(update, context)
            return

        if text == BTN_SMARTDASH:
            await addons.cmd_smartdash(update, context)
            return

        if text == BTN_HIDE_ADMIN:
            await _set_admin_kb_hidden(True)
            await update.message.reply_text(
                "🙈 تم إخفاء أزرار الأدمن.",
                reply_markup=get_keyboard_for(user_id))
            return

        if text == BTN_SHOW_ADMIN:
            await _set_admin_kb_hidden(False)
            await update.message.reply_text(
                "👁️ تم إظهار أزرار الأدمن.",
                reply_markup=get_keyboard_for(user_id))
            return

        if text == BTN_HIDE_FEATURES:
            await _set_features_kb_hidden(True)
            await update.message.reply_text(
                "🙈 تم إخفاء أزرار الأدوات من كل المستخدمين.\n"
                "استخدم زر 👁️ إظهار الأدوات لإعادتها.",
                reply_markup=get_keyboard_for(user_id))
            return

        if text == BTN_SHOW_FEATURES:
            await _set_features_kb_hidden(False)
            await update.message.reply_text(
                "👁️ تم إظهار أزرار الأدوات.",
                reply_markup=get_keyboard_for(user_id))
            return

        if text == BTN_ADMIN_PANEL:
            panel_message = await update.message.reply_text(
                await build_admin_panel_text(), parse_mode="HTML",
                reply_markup=build_admin_keyboard_inline())
            context.user_data["admin_panel_message_id"] = panel_message.message_id
            return

        if text == BTN_MANAGE_USERS:
            total = await db.count_allowed_users()
            if total == 0:
                await update.message.reply_text("📭 مفيش مستخدمين مضافين.")
                return
            panel_text, kb = await build_users_list_inline(0, bot=context.bot)
            await update.message.reply_text(panel_text, parse_mode="HTML", reply_markup=kb)
            return

        if text == BTN_MANAGE_CHATS:
            await send_registered_chats(update.message, context)
            return

        if text == BTN_ANALYTICS:
            await update.message.reply_text(
                await build_analytics_text(), parse_mode="HTML")
            return

        if text == BTN_BOT_STATUS:
            state = "✅ شغال" if _BOT_ENABLED else "🛑 متوقف"
            now = time.time()
            active_keys = (sum(1 for i in range(len(KEY_POOL))
                               if KEY_POOL.cooldown_until.get(i, 0) <= now)
                           if KEY_POOL else 0)
            total_u = await db.count_allowed_users()
            await update.message.reply_text(
                f"📊 <b>حالة البوت</b>\n\n"
                f"الحالة: {state}\n"
                f"المستخدمين: {total_u}\n"
                f"مفاتيح Gemini: {active_keys}/{len(KEY_POOL) if KEY_POOL else 0} نشطة\n"
                f"وضع التشغيل: {'Webhook' if WEBHOOK_URL else 'Polling'}",
                parse_mode="HTML")
            return

        if text == BTN_BROADCAST:
            context.user_data["admin_action"] = "awaiting_broadcast"
            touch_state(context)
            total = await db.count_allowed_users()
            await update.message.reply_text(
                f"📢 <b>رسالة جماعية</b>\n\n"
                f"اكتب الرسالة لكل المستخدمين ({total}).\n\n"
                f"<i>/cancel للإلغاء</i>", parse_mode="HTML")
            return

        if text == BTN_MEDIA_BROADCAST:
            context.user_data["admin_action"] = "awaiting_media_target"
            touch_state(context)
            await update.message.reply_text(
                "📸🎬 اكتب <code>all</code> لكل المستخدمين أو ابعت User ID واحد.",
                parse_mode="HTML")
            return

        if text == BTN_TOGGLE_BOT:
            await _set_bot_enabled(not _BOT_ENABLED)
            state = "✅ شغال" if _BOT_ENABLED else "🛑 متوقف"
            await update.message.reply_text(
                f"تم التغيير، البوت دلوقتي: {state}",
                reply_markup=get_keyboard_for(user_id))
            return

    # ── Smart chat pass-through ──
    if context.user_data.get("mode") == "smart_chat":
        if not await ensure_access(update):
            return
        await handle_smart_chat(update, context, text)
        return

    if not await ensure_access(update):
        return
    await update.message.reply_text(
        "اختار حاجة من الأزرار تحت 👇\nأو ابعت PDF/صورة عشان أعمل أسئلة.",
        reply_markup=get_keyboard_for(user_id))


# ============================================================
# PDF text + image extraction (unchanged from v3)
# ============================================================
class PdfTooLargeError(Exception):
    pass


def extract_pdf_text_pdfplumber(pdf_stream) -> str:
    parts = []
    with pdfplumber.open(pdf_stream) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                parts.append(page_text)
    return "\n".join(parts)


def extract_pdf_text_and_images(
    pdf_bytes: bytes, max_images: int = MAX_IMAGES_PER_PDF,
) -> "tuple[str, list[tuple[bytes, str]]]":
    text_parts: list[str] = []
    images: list[tuple[bytes, str]] = []
    seen_xrefs: set[int] = set()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        logger.exception("fitz.open failed, falling back to pdfplumber")
        try:
            text = extract_pdf_text_pdfplumber(BytesIO(pdf_bytes))
        except Exception:
            text = ""
        return text, []

    try:
        page_count = len(doc)
        if page_count > MAX_PDF_PAGES:
            doc.close()
            raise PdfTooLargeError(f"عدد الصفحات {page_count}")
    except PdfTooLargeError:
        raise
    except Exception:
        page_count = 0

    try:
        for page_num, page in enumerate(doc):
            try:
                t = page.get_text()
                if t and t.strip():
                    text_parts.append(t)
            except Exception:
                logger.warning("Failed to extract text from page %d", page_num)

            if len(images) >= max_images:
                continue

            try:
                img_list = page.get_images(full=True)
            except Exception:
                img_list = []

            for img_info in img_list:
                if len(images) >= max_images:
                    break
                xref = img_info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    base = doc.extract_image(xref)
                    raw = base.get("image")
                    if not raw:
                        continue
                    ext = (base.get("ext") or "png").lower()
                    if ext == "jpg":
                        ext = "jpeg"
                    if ext not in ("jpeg", "png", "webp", "gif", "bmp"):
                        continue
                    if len(raw) < MIN_IMAGE_BYTES:
                        continue
                    mime = f"image/{ext}"
                    if ext == "bmp":
                        mime = "image/bmp"
                    images.append((raw, mime))
                except Exception:
                    continue
    finally:
        try:
            doc.close()
        except Exception:
            pass

    full_text = "\n".join(text_parts)
    if not full_text.strip():
        try:
            full_text = extract_pdf_text_pdfplumber(BytesIO(pdf_bytes))
        except Exception:
            logger.exception("pdfplumber fallback failed for empty-text PDF")

    logger.info("PDF extracted: %d chars text, %d images, %d pages",
                len(full_text), len(images), page_count)
    return full_text, images


# ============================================================
# Gemini — MCQ Generation (unchanged from v3)
# ============================================================
MCQ_SCHEMA = types.Schema(
    type=types.Type.ARRAY,
    items=types.Schema(
        type=types.Type.OBJECT,
        required=["q", "a", "b", "c", "d", "answer"],
        properties={
            "q":      types.Schema(type=types.Type.STRING),
            "a":      types.Schema(type=types.Type.STRING),
            "b":      types.Schema(type=types.Type.STRING),
            "c":      types.Schema(type=types.Type.STRING),
            "d":      types.Schema(type=types.Type.STRING),
            "answer": types.Schema(type=types.Type.STRING, enum=["A", "B", "C", "D"]),
        },
    ),
)


def build_mcq_prompt(n_questions: int, difficulty: str, language: str,
                     text_block: Optional[str] = None,
                     has_images: bool = False,
                     image_only: bool = False) -> str:
    n_cases = max(3, n_questions // 5)
    if image_only:
        source_block = "\n\nLECTURE CONTENT: (see attached image)\n"
    elif text_block and has_images:
        source_block = (
            f'\n\nLECTURE TEXT (extracted from the PDF):\n"""\n{text_block}\n"""\n\n'
            f'ALSO consider the {"images" if has_images else "image"} attached — '
            f'they are figures/diagrams/photos embedded in the PDF. '
            f'Use them as part of the lecture content when writing questions.\n'
        )
    elif text_block:
        source_block = f'\n\nLECTURE CONTENT:\n"""\n{text_block}\n"""\n'
    else:
        source_block = "\n\nLECTURE CONTENT: (see attached images)\n"

    return f"""You are an expert academic exam writer.

From the LECTURE CONTENT below, write EXACTLY {n_questions} high-quality
multiple-choice questions (MCQs).

Strict rules:
- Each question has 4 options labeled A, B, C, D.
- Exactly ONE option is correct.
- Distractors must be plausible (not obvious throwaways).
- Cover different topics throughout the lecture; do NOT repeat questions.
- VARY which option is correct across the question set. The correct answer
  letter should be roughly evenly distributed across A, B, C, D (about 25%
  each).

Difficulty:
- {DIFFICULTY_INSTRUCTIONS[difficulty]}

Case-based questions:
- Include approximately {n_cases} CASE-BASED / scenario questions, placed
  toward the END of the set.

Language:
- {LANG_INSTRUCTIONS[language]}

Length limits:
- Keep each question under 280 characters.
- Keep each option under 95 characters.

Return a JSON array of objects with fields: q, a, b, c, d, answer
where "answer" is one of "A", "B", "C", "D".{source_block}"""


def _parse_mcq_response(response) -> list:
    if getattr(response, "prompt_feedback", None) and response.prompt_feedback.block_reason:
        logger.error("Prompt blocked: %s", response.prompt_feedback.block_reason)
        return []
    if not response.candidates:
        return []
    raw = (response.text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON parse failed: %s | snippet: %s", e, raw[:400])
        return []
    if not isinstance(data, list):
        return []
    cleaned = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not all(k in item for k in ("q", "a", "b", "c", "d", "answer")):
            continue
        if str(item["answer"]).upper() not in {"A", "B", "C", "D"}:
            continue
        cleaned.append(item)
    logger.info("Generated %d valid MCQs", len(cleaned))
    return cleaned


def _gen_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=MCQ_SCHEMA,
        temperature=0.8,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


async def generate_mcqs_from_text(text, n, diff, lang):
    prompt = build_mcq_prompt(n, diff, lang, text_block=text)
    r = await call_gemini(model=GEMINI_MODEL, contents=prompt, config=_gen_config())
    return _parse_mcq_response(r)


async def generate_mcqs_from_image(image_bytes, mime, n, diff, lang):
    prompt = build_mcq_prompt(n, diff, lang, text_block=None, image_only=True)
    r = await call_gemini(
        model=GEMINI_MODEL,
        contents=[types.Part.from_bytes(data=image_bytes,
                                        mime_type=mime or "image/jpeg"), prompt],
        config=_gen_config(),
    )
    return _parse_mcq_response(r)


async def generate_mcqs_from_pdf(text, images, n, diff, lang):
    has_images = bool(images)
    text_for_prompt = text if text and text.strip() else None
    prompt = build_mcq_prompt(n, diff, lang,
                              text_block=text_for_prompt, has_images=has_images,
                              image_only=(not text_for_prompt and has_images))
    contents = []
    for img_bytes, mime in images:
        try:
            contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
        except Exception:
            pass
    contents.append(prompt)
    r = await call_gemini(model=GEMINI_MODEL, contents=contents, config=_gen_config())
    return _parse_mcq_response(r)


# ============================================================
# Send quizzes
# ============================================================
def truncate(text: str, limit: int) -> str:
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def shuffle_options(options: list, correct_idx: int):
    indexed = list(enumerate(options))
    random.shuffle(indexed)
    new_options = [opt for _, opt in indexed]
    new_correct = next(
        new_i for new_i, (old_i, _) in enumerate(indexed) if old_i == correct_idx
    )
    return new_options, new_correct


async def send_quizzes(chat, mcqs: list, user_id: int):
    u = await db.get_user(user_id)
    is_anonymous    = bool(u.anonymous) if u else True
    protect_content = bool(u.protect_content) if u else False

    answer_index = {"A": 0, "B": 1, "C": 2, "D": 3}
    sent = 0
    skipped = 0

    for i, q in enumerate(mcqs, 1):
        question_text = truncate(f"Q{i}: {q['q']}", MAX_QUESTION_LEN)
        options = [
            truncate(q["a"], MAX_OPTION_LEN),
            truncate(q["b"], MAX_OPTION_LEN),
            truncate(q["c"], MAX_OPTION_LEN),
            truncate(q["d"], MAX_OPTION_LEN),
        ]
        original_correct = answer_index[str(q["answer"]).upper()]
        if any(not opt for opt in options):
            skipped += 1
            continue
        shuffled, new_correct = shuffle_options(options, original_correct)
        try:
            await chat.send_poll(
                question=question_text, options=shuffled,
                type="quiz", correct_option_id=new_correct,
                is_anonymous=is_anonymous, protect_content=protect_content,
            )
            sent += 1
        except Exception as e:
            logger.warning("Failed to send quiz #%d: %s", i, e)
            skipped += 1
            continue
        await asyncio.sleep(0.3)

    summary = f"عملتلك {sent} سؤال، فرهدتوني كفاية بقي 🫠"
    if skipped:
        summary += f"\n⚠️ تم تخطي {skipped} سؤال"
    await chat.send_message(summary)


# ============================================================
# Error handler
# ============================================================
_alert_throttle: dict[str, float] = {}
ALERT_COOLDOWN = 30


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    logger.exception("Unhandled exception in handler", exc_info=err)
    if not ADMIN_IDS:
        return
    err_key = f"{type(err).__name__}:{str(err)[:80]}"
    now = time.time()
    if now - _alert_throttle.get(err_key, 0) < ALERT_COOLDOWN:
        return
    _alert_throttle[err_key] = now
    admin_id = next(iter(ADMIN_IDS))
    try:
        user_line = ""
        if isinstance(update, Update) and update.effective_user:
            u = update.effective_user
            who = html_escape(u.username or u.first_name or "—")
            user_line = f"\nUser: <code>{u.id}</code> ({who})"
        err_text = f"{type(err).__name__}: {err}"
        if len(err_text) > 3500:
            err_text = err_text[:3500] + "…"
        msg = (f"⚠️ <b>Bot Error Alert</b>{user_line}\n\n"
               f"<pre>{html_escape(err_text)}</pre>")
        await context.bot.send_message(admin_id, msg, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send admin alert")


# ============================================================
# Startup hooks
# ============================================================
async def auto_migrate_on_startup():
    """If the DB is empty but JSON files exist next to us, run migration."""
    total = await db.count_allowed_users()
    if total > 0:
        return
    data_dir = Path(os.environ.get("BOT_DATA_DIR", ".")).resolve()
    json_files = [data_dir / f for f in
                  ("allowed_users.json", "user_settings.json",
                   "user_quotas.json", "user_info.json")]
    if not any(p.exists() for p in json_files):
        return
    logger.info("Empty DB + JSON files found → auto-migrating from %s", data_dir)
    try:
        await run_json_migration(data_dir, dry_run=False)
    except Exception:
        logger.exception("Auto-migration failed; continuing anyway")


async def post_init(app: Application):
    await db.init_db()
    await auto_migrate_on_startup()
    await _load_flags()
    logger.info("DB ready. enabled=%s, admin_kb_hidden=%s",
                _BOT_ENABLED, _ADMIN_KB_HIDDEN)


async def post_shutdown(app: Application):
    await db.close_db()


# ============================================================
# Entry point
# ============================================================
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")
    if not GEMINI_KEYS:
        raise RuntimeError(
            "No Gemini API keys configured. Set GEMINI_API_KEY (single) "
            "or GEMINI_API_KEYS=key1,key2,... (up to 20)."
        )

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)          # v5: true multi-task processing
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("myid",   myid))
    app.add_handler(CommandHandler("admin",  admin_panel))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("stats",  stats_cmd))
    app.add_handler(CommandHandler("register_chat", register_chat_cmd))
    app.add_handler(CommandHandler("medical_chats", registered_chats_cmd))

    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL,
            handle_admin_media_broadcast,
        ),
        group=-6,
    )
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))

    app.add_handler(CallbackQueryHandler(on_admin_action,    pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_settings_action, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(on_language,        pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_difficulty,      pattern=r"^diff:"))
    app.add_handler(CallbackQueryHandler(on_cancel_flow,     pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(on_export,          pattern=r"^export:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    # v5: register the add-on feature modules (commands, callbacks and the
    # high-priority document/photo interceptors for Spot / Quiz-from-file).
    import medical, pdf_restyle, virtual_patient
    addons.register(app)
    islamic.register(app)            # uses the new islamic.py
    medical.register(app)            # group -2: runs before addons' interceptors
    pdf_restyle.register(app)        # group -4: intercepts PDF restyle uploads
    virtual_patient.register(app)

    if WEBHOOK_URL:
        url = WEBHOOK_URL.rstrip("/") + "/" + WEBHOOK_PATH
        logger.info("Bot starting in WEBHOOK mode (model=%s, keys=%d) on %s:%d → %s",
                    GEMINI_MODEL, len(GEMINI_KEYS),
                    WEBHOOK_LISTEN, WEBHOOK_PORT, url)
        app.run_webhook(
            listen=WEBHOOK_LISTEN,
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=url,
            secret_token=WEBHOOK_SECRET,
        )
    else:
        logger.info("Bot starting in POLLING mode (model=%s, keys=%d) …",
                    GEMINI_MODEL, len(GEMINI_KEYS))
        app.run_polling()


if __name__ == "__main__":
    main()
