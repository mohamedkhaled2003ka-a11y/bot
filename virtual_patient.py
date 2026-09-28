"""Menu-driven interactive virtual patient simulator for medical education."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

logger = logging.getLogger("virtual_patient")
MODE = "virtual_patient"
MAX_TURNS = 40
SPECIALTIES = {
    "internal": "Internal Medicine",
    "cardiology": "Cardiology",
    "neurology": "Neurology",
    "respiratory": "Respiratory Medicine",
    "gastro": "Gastroenterology",
    "infectious": "Infectious Diseases",
    "surgery": "Surgery",
    "pediatrics": "Pediatrics",
    "obgyn": "Obstetrics and Gynecology",
    "emergency": "Emergency Medicine",
    "random": "Random Specialty",
}
DIFFICULTIES = {
    "easy": "Easy",
    "medium": "Medium",
    "hard": "Hard",
    "random": "Random",
}

_CASE_PROMPT = """Create one medically coherent clinical case for a medical student.
Return JSON only. The patient must initially volunteer only the opening complaint.
Keep hidden information private until the learner asks an appropriate question.
Include a realistic but safe educational case, coherent findings, and a scoring rubric.
"""
_PATIENT_PROMPT = """You are the patient in a clinical history-taking simulation.
Answer in first person as the patient, in the learner's language. Reveal only the
specific history, examination, or investigation result requested. Do not volunteer
unasked information, diagnosis, differential, or teaching points. Remain consistent
with the private case. If the learner asks for an examination or investigation,
return only the relevant requested result and label it as a simulated result.
Do not provide treatment advice. This is educational simulation only.
"""
_EVALUATOR_PROMPT = """Evaluate a medical student's virtual-patient session.
Score out of 100 using: history completeness/relevance 30, examination requests 15,
investigations 15, final diagnosis 30, clinical reasoning/differential 10.
Do not penalize order, harmless wording, or extra relevant questions. Accept synonyms
and abbreviations. A correct diagnosis should receive substantial credit. Return JSON
only with integer total_score, integer category scores, feedback, correct_diagnosis,
missed_essentials, and debrief.
"""


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🩺 تاريخ مرضي", callback_data="vp:history"),
         InlineKeyboardButton("🩻 فحص إكلينيكي", callback_data="vp:exam")],
        [InlineKeyboardButton("🧪 تحقيقات", callback_data="vp:investigation"),
         InlineKeyboardButton("🧠 تقديم التشخيص", callback_data="vp:diagnosis")],
        [InlineKeyboardButton("📊 إنهاء وتقييم", callback_data="vp:finish")],
        [InlineKeyboardButton("❌ خروج", callback_data="vp:exit")],
    ])


def _main_menu(has_active: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if has_active:
        rows.append([InlineKeyboardButton("▶️ متابعة الحالة الحالية", callback_data="vp:continue")])
    rows.extend([
        [InlineKeyboardButton("🆕 حالة جديدة", callback_data="vp:new_menu")],
        [InlineKeyboardButton("🎯 اختيار التخصص", callback_data="vp:specialty")],
        [InlineKeyboardButton("⚙️ اختيار الصعوبة", callback_data="vp:difficulty")],
        [InlineKeyboardButton("📈 درجاتي السابقة", callback_data="vp:scores")],
        [InlineKeyboardButton("❌ خروج", callback_data="vp:exit")],
    ])
    return InlineKeyboardMarkup(rows)


def _specialty_keyboard() -> InlineKeyboardMarkup:
    rows = []
    items = list(SPECIALTIES.items())
    for index in range(0, len(items), 2):
        rows.append([
            InlineKeyboardButton(label, callback_data=f"vp:set_specialty:{key}")
            for key, label in items[index:index + 2]
        ])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="vp:menu")])
    return InlineKeyboardMarkup(rows)


def _difficulty_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"vp:set_difficulty:{key}")]
        for key, label in DIFFICULTIES.items()
    ] + [[InlineKeyboardButton("🔙 رجوع", callback_data="vp:menu")]])


def _html(bot, value) -> str:
    return bot.html_escape(str(value or ""))


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _normalise_json(value) -> dict | None:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif value is not None and not isinstance(value, dict):
        try:
            value = dict(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def _case_schema(types):
    string_list = types.Schema(type=types.Type.ARRAY,
                                items=types.Schema(type=types.Type.STRING))
    hidden_case = types.Schema(
        type=types.Type.OBJECT,
        required=["demographics", "history", "examination", "investigations", "diagnosis"],
        properties={
            "demographics": types.Schema(type=types.Type.STRING),
            "history": types.Schema(type=types.Type.STRING),
            "examination": types.Schema(type=types.Type.STRING),
            "investigations": types.Schema(type=types.Type.STRING),
            "diagnosis": types.Schema(type=types.Type.STRING),
        },
    )
    return types.Schema(
        type=types.Type.OBJECT,
        required=["case_id", "specialty", "difficulty", "opening", "hidden_case",
                  "examination", "investigations", "diagnosis", "synonyms",
                  "differential", "key_history", "essential_exams",
                  "appropriate_investigations", "rubric", "explanation",
                  "red_flags", "learning_objectives"],
        properties={
            "case_id": types.Schema(type=types.Type.STRING),
            "specialty": types.Schema(type=types.Type.STRING),
            "difficulty": types.Schema(type=types.Type.STRING),
            "opening": types.Schema(type=types.Type.STRING),
            "hidden_case": hidden_case,
            "examination": types.Schema(type=types.Type.OBJECT),
            "investigations": types.Schema(type=types.Type.OBJECT),
            "diagnosis": types.Schema(type=types.Type.STRING),
            "synonyms": string_list,
            "differential": string_list,
            "key_history": string_list,
            "essential_exams": string_list,
            "appropriate_investigations": string_list,
            "rubric": types.Schema(type=types.Type.OBJECT),
            "explanation": types.Schema(type=types.Type.STRING),
            "red_flags": string_list,
            "learning_objectives": string_list,
        },
    )


def _parse_json(response) -> dict:
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        value = _normalise_json(parsed)
    else:
        raw = (getattr(response, "text", "") or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        value = _normalise_json(json.loads(raw))
    if not value:
        raise ValueError("Gemini returned an empty structured response")
    return value


async def _generate_case(specialty: str, difficulty: str) -> dict:
    import bot
    from google.genai import types
    requested_specialty = SPECIALTIES.get(specialty, specialty)
    requested_difficulty = DIFFICULTIES.get(difficulty, difficulty)
    prompt = (f"{_CASE_PROMPT}\nSpecialty: {requested_specialty}\n"
              f"Difficulty: {requested_difficulty}\n"
              "hidden_case must contain demographics, history, examination, and investigation results. "
              "Keep every field concise and return valid JSON without markdown fences.")
    case = None
    last_error = None
    for attempt in range(1):
        try:
            response = await bot.call_gemini(
                model=bot.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_case_schema(types),
                    temperature=0.35,
                    max_output_tokens=5000,
                ),
            )
            candidate = _parse_json(response)
            candidate.setdefault("case_id", uuid.uuid4().hex[:12])
            candidate["specialty"] = str(candidate.get("specialty") or requested_specialty)
            candidate["difficulty"] = str(candidate.get("difficulty") or requested_difficulty)
            hidden = candidate.get("hidden_case")
            if isinstance(hidden, dict):
                candidate["diagnosis"] = str(candidate.get("diagnosis") or hidden.get("diagnosis") or "Undifferentiated clinical syndrome")
                if not candidate.get("opening"):
                    candidate["opening"] = str(hidden.get("history") or "I have come because I do not feel well.")
            if not candidate.get("opening") or not candidate.get("hidden_case") or not candidate.get("diagnosis"):
                raise ValueError("Generated case is missing required clinical data")
            case = candidate
            break
        except Exception as exc:
            last_error = exc
            logger.warning("Malformed virtual patient case attempt %d: %s", attempt + 1, exc)
            prompt = (f"Create a SHORT valid JSON clinical case. Specialty: {requested_specialty}. "
                      f"Difficulty: {requested_difficulty}. Include only concise values for every schema field. "
                      "No markdown, no commentary, JSON only.")
    if case is None:
        logger.warning("Using fallback virtual patient case after Gemini failure: %s", last_error)
        return _fallback_case(requested_specialty, requested_difficulty)
    return case


def _fallback_case(specialty: str, difficulty: str) -> dict:
    actual_specialty = "Cardiology" if specialty == "Random Specialty" else specialty
    return {
        "case_id": "fallback-chest-pain",
        "specialty": actual_specialty,
        "difficulty": difficulty,
        "opening": "My name is Ahmed. I am 54 years old and I have had chest pain since this morning.",
        "hidden_case": {
            "demographics": "54-year-old man",
            "history": "Central crushing chest pain for two hours, radiating to the left arm, with sweating and nausea. History of hypertension and smoking.",
            "examination": "Anxious, pulse 104, blood pressure 158/94, oxygen saturation 96% on room air.",
            "investigations": "ECG shows ST elevation in leads II, III and aVF. Troponin is elevated.",
            "diagnosis": "Acute inferior ST-elevation myocardial infarction",
        },
        "examination": {},
        "investigations": {},
        "diagnosis": "Acute inferior ST-elevation myocardial infarction",
        "synonyms": ["Acute MI", "Inferior STEMI", "Acute myocardial infarction"],
        "differential": ["Aortic dissection", "Pulmonary embolism", "Acute pericarditis"],
        "key_history": ["Onset and character of pain", "Radiation", "Risk factors", "Associated symptoms"],
        "essential_exams": ["Vital signs", "Cardiovascular examination"],
        "appropriate_investigations": ["ECG", "Cardiac troponin", "CBC", "Renal function"],
        "rubric": {"history": 30, "examination": 15, "investigations": 15, "diagnosis": 30, "reasoning": 10},
        "explanation": "The prolonged crushing chest pain with autonomic symptoms and inferior ST elevation is consistent with inferior STEMI.",
        "red_flags": ["Ongoing chest pain", "Hemodynamic instability", "Arrhythmia"],
        "learning_objectives": ["Take focused chest-pain history", "Recognize STEMI red flags", "Choose urgent ECG and troponin"],
    }


def _session(case: dict, specialty: str, difficulty: str) -> dict:
    return {
        "case_id": case.get("case_id", uuid.uuid4().hex[:12]),
        "specialty": specialty,
        "difficulty": difficulty,
        "case": case,
        "history": [],
        "examinations": [],
        "investigations": [],
        "started_at": _now(),
        "completed": False,
    }


def _store(context, session: dict):
    context.user_data["virtual_patient_session"] = session
    context.user_data["mode"] = MODE


async def _save(session: dict, user_id: int):
    try:
        await db.save_virtual_patient_session(user_id, session)
    except Exception:
        logger.exception("Could not persist virtual patient session")


async def _load_active(context, user_id: int) -> dict | None:
    session = await db.get_virtual_patient_session(user_id, active_only=True)
    if session:
        _store(context, session)
    return session


async def _send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    active = await _load_active(context, update.effective_user.id)
    await update.effective_message.reply_text(
        "🩺 <b>Virtual Interactive Patient</b>\n\n"
        "تدرّب على أخذ التاريخ المرضي، الفحص، التحقيقات، والتشخيص.\n"
        "كل الحالات والنتائج محاكاة تعليمية.",
        parse_mode="HTML",
        reply_markup=_main_menu(bool(active)),
    )


async def cmd_virtual_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not await bot.ensure_access(update):
        return
    if not bot.is_admin(update.effective_user.id) and not bot._VIRTUAL_PATIENT_ENABLED:
        await update.message.reply_text("ميزة المريض الافتراضي متوقفة حاليًا من الأدمن.")
        return
    await _send_menu(update, context)


async def button_virtual_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_virtual_patient(update, context)
    raise ApplicationHandlerStop


async def button_toggle_virtual_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    if not bot.is_admin(update.effective_user.id):
        raise ApplicationHandlerStop
    await bot._set_virtual_patient_enabled(not bot._VIRTUAL_PATIENT_ENABLED)
    state = "✅ شغال" if bot._VIRTUAL_PATIENT_ENABLED else "🛑 متوقف"
    await update.message.reply_text(
        f"🩺 المريض الافتراضي: {state}",
        reply_markup=bot.get_keyboard_for(update.effective_user.id),
    )
    raise ApplicationHandlerStop


async def _start_case(query, context, specialty: str, difficulty: str):
    import bot
    await query.edit_message_text("🩺 جاري تجهيز الحالة الطبية…")
    try:
        case = await _generate_case(specialty, difficulty)
        session = _session(case, specialty, difficulty)
        _store(context, session)
        await _save(session, query.from_user.id)
        await query.message.reply_text(
            f"🩺 <b>{_html(bot, case['specialty'])}</b> | {_html(bot, case['difficulty'])}\n\n"
            f"{_html(bot, case['opening'])}\n\n"
            "اسأل المريض طبيعيًا، أو استخدم أزرار الفحص والتحقيقات.\n"
            "⚠️ محاكاة تعليمية وليست رعاية طبية.",
            parse_mode="HTML", reply_markup=_keyboard())
    except Exception as exc:
        logger.exception("Virtual patient generation failed")
        await query.message.reply_text(f"❌ مقدرتش أجهز الحالة: {_html(bot, exc)}")


async def _scores(query, user_id: int):
    import bot
    scores = await db.list_virtual_patient_scores(user_id, limit=10)
    if not scores:
        await query.edit_message_text("📈 مفيش حالات مكتملة لسه.", reply_markup=_main_menu())
        return
    lines = ["📈 <b>درجاتك السابقة</b>", ""]
    for item in scores:
        lines.append(f"• {_html(bot, item['specialty'])} / {_html(bot, item['difficulty'])}: "
                     f"<b>{item['final_score']}/100</b> — {item['ended_at']}")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=_main_menu())


async def _patient_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    import bot
    session = context.user_data.get("virtual_patient_session")
    if not session or session.get("completed"):
        await update.message.reply_text("مفيش حالة نشطة. افتح 🩺 مريض افتراضي وابدأ حالة جديدة.")
        return
    if len(session["history"]) >= MAX_TURNS:
        await update.message.reply_text("وصلت للحد الأقصى من الأسئلة. اضغط إنهاء وتقييم.", reply_markup=_keyboard())
        return
    lowered = text.lower()
    kind = context.user_data.pop("vp_pending", "history")
    if kind == "history" and any(word in lowered for word in ("examination", "exam", "فحص", "علامات حيوية", "vital")):
        kind = "examination"
        session["examinations"].append(text)
    elif kind == "history" and any(word in lowered for word in ("test", "investigation", "تحليل", "أشعة", "ecg", "cbc", "فحص دم")):
        kind = "investigation"
        session["investigations"].append(text)
    session["history"].append({"role": "learner", "text": text, "kind": kind})
    reply = _local_patient_reply(session["case"], kind, text)
    session["history"].append({"role": "patient", "text": reply, "kind": kind})
    await _save(session, update.effective_user.id)
    await update.message.reply_text(reply, reply_markup=_keyboard())


def _local_patient_reply(case: dict, kind: str, question: str) -> str:
    hidden = case.get("hidden_case") or {}
    if kind == "examination":
        result = hidden.get("examination") or "No specific examination finding is available for that request."
        return f"🩻 Simulated examination result:\n{result}"
    if kind == "investigation":
        result = hidden.get("investigations") or "No result is available for that investigation yet."
        return f"🧪 Simulated investigation result:\n{result}"
    lowered = question.lower()
    if any(word in lowered for word in ("name", "age", "old", "اسم", "سن", "كام سنة")):
        return str(hidden.get("demographics") or "I am an adult patient.")
    if any(word in lowered for word in ("when", "how long", "onset", "متى", "امتى", "منذ", "مدة")):
        return str(hidden.get("history") or "It started recently.")
    if any(word in lowered for word in ("diagnosis", "تشخيص", "التشخيص", "diagnose")):
        return "I am not sure what the diagnosis is, doctor."
    return str(hidden.get("history") or "I can tell you more if you ask me a specific question.")


async def _submit_diagnosis(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    session = context.user_data.get("virtual_patient_session")
    if not session:
        await update.message.reply_text("مفيش حالة نشطة.")
        return
    session["history"].append({"role": "learner", "text": text[:1800],
                               "kind": "final_diagnosis"})
    context.user_data.pop("vp_pending", None)
    await _save(session, update.effective_user.id)
    await update.message.reply_text(
        "✅ تم تسجيل التشخيص. يمكنك إضافة differential/reasoning ثم اضغط إنهاء وتقييم.",
        reply_markup=_keyboard())


async def _evaluate(session: dict) -> dict:
    return _local_evaluation(session)


def _local_evaluation(session: dict) -> dict:
    """Keep a completed score available when the evaluator response is unusable."""
    case = session.get("case", {})
    history = session.get("history", [])
    learner_text = " ".join(
        str(item.get("text", "")) for item in history if item.get("role") == "learner"
    ).lower()
    questions = [item for item in history if item.get("role") == "learner"
                 and item.get("kind") in ("history", "examination", "investigation")]
    diagnosis = str(case.get("diagnosis", "Undifferentiated clinical syndrome"))
    synonyms = [diagnosis, *(case.get("synonyms") or [])]
    diagnosis_score = 30 if any(term.lower() in learner_text for term in synonyms if term) else 0
    history_score = min(30, len(questions) * 4)
    examination_score = min(15, len(session.get("examinations", [])) * 8)
    investigation_score = min(15, len(session.get("investigations", [])) * 5)
    reasoning_score = 10 if any(word in learner_text for word in ("because", "differential", "because", "لأن", "تفريقي")) else 4
    return {
        "history_score": history_score,
        "examination_score": examination_score,
        "investigation_score": investigation_score,
        "diagnosis_score": diagnosis_score,
        "reasoning_score": reasoning_score,
        "feedback": "تم استخدام تقييم احتياطي؛ راجع الأسئلة الأساسية والتشخيص المرجعي.",
        "correct_diagnosis": diagnosis,
        "missed_essentials": case.get("key_history", [])[:5],
        "debrief": case.get("explanation", "راجع ترابط الأعراض والفحص والتحقيقات."),
    }


async def _finish(query, context):
    import bot
    session = context.user_data.get("virtual_patient_session")
    if not session:
        await query.edit_message_text("مفيش حالة نشطة.", reply_markup=_main_menu())
        return
    await query.edit_message_text("📊 جاري تقييم التاريخ المرضي والتشخيص…")
    try:
        score = await _evaluate(session)
    except Exception as exc:
        logger.exception("Virtual patient evaluation failed")
        await query.message.reply_text(f"❌ مقدرتش أقيّم الحالة: {_html(bot, exc)}")
        return
    session.update({"completed": True, "ended_at": _now(), "final_score": score["total_score"],
                    "score": score, "feedback": score.get("feedback", "")})
    await _save(session, query.from_user.id)
    context.user_data.pop("mode", None)
    context.user_data.pop("virtual_patient_session", None)
    context.user_data.pop("vp_pending", None)
    lines = [
        "🧾 <b>تقييم Virtual Patient</b>", "",
        f"النتيجة: <b>{score['total_score']}/100</b>",
        f"التاريخ المرضي: {score['history_score']}/30",
        f"الفحص: {score['examination_score']}/15",
        f"التحقيقات: {score['investigation_score']}/15",
        f"التشخيص: {score['diagnosis_score']}/30",
        f"التفكير السريري: {score['reasoning_score']}/10", "",
        f"<b>التشخيص المرجعي:</b> {_html(bot, score.get('correct_diagnosis'))}",
        f"<b>التغذية الراجعة:</b> {_html(bot, score.get('feedback'))}",
        f"<b>ما فاتك:</b> {_html(bot, ', '.join(score.get('missed_essentials') or []))}",
        f"<b>Debrief:</b> {_html(bot, score.get('debrief'))}", "",
        "⚠️ محاكاة تعليمية وليست بديلًا عن الإشراف الطبي.",
    ]
    await query.message.reply_text("\n".join(lines), parse_mode="HTML",
                                   reply_markup=_main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if context.user_data.get("mode") != MODE:
        return False
    if text.strip() == "/cancel":
        context.user_data.pop("mode", None)
        context.user_data.pop("virtual_patient_session", None)
        await update.message.reply_text("تم إنهاء الحالة ✅")
        return True
    if context.user_data.get("vp_pending") == "diagnosis":
        await _submit_diagnosis(update, context, text.strip())
        return True
    await _patient_turn(update, context, text.strip())
    return True


async def _callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import bot
    query = update.callback_query
    await query.answer()
    if not await bot.ensure_access(update):
        return
    action = query.data.split(":")
    name = action[1]
    if name == "menu":
        await _send_menu(update, context)
    elif name == "specialty":
        await query.edit_message_text("🎯 اختار التخصص:", reply_markup=_specialty_keyboard())
    elif name == "difficulty":
        await query.edit_message_text("⚙️ اختار الصعوبة:", reply_markup=_difficulty_keyboard())
    elif name == "set_specialty":
        context.user_data["vp_specialty"] = action[2]
        await query.edit_message_text("تم اختيار التخصص. اختار الصعوبة:", reply_markup=_difficulty_keyboard())
    elif name == "set_difficulty":
        context.user_data["vp_difficulty"] = action[2]
        await query.edit_message_text("تم اختيار الصعوبة. اضغط حالة جديدة للبدء.", reply_markup=_main_menu())
    elif name == "new_menu":
        specialty = context.user_data.get("vp_specialty", "random")
        difficulty = context.user_data.get("vp_difficulty", "random")
        await _start_case(query, context, specialty, difficulty)
    elif name == "continue":
        session = await _load_active(context, query.from_user.id)
        if session:
            await query.edit_message_text("▶️ استأنفت الحالة. اسأل المريض أو استخدم الأزرار.", reply_markup=_keyboard())
        else:
            await query.edit_message_text("مفيش حالة نشطة.", reply_markup=_main_menu())
    elif name == "scores":
        await _scores(query, query.from_user.id)
    elif name in ("history", "exam", "investigation", "diagnosis"):
        prompts = {
            "history": "اكتب سؤالك للمريض عن التاريخ المرضي.",
            "exam": "اكتب الفحص المطلوب، مثل: vital signs أو chest examination.",
            "investigation": "اكتب التحقيق المطلوب، مثل: CBC أو ECG أو chest X-ray.",
            "diagnosis": "اكتب تشخيصك النهائي مع differential/reasoning إن أمكن.",
        }
        context.user_data["vp_pending"] = {
            "history": "history",
            "exam": "examination",
            "investigation": "investigation",
            "diagnosis": "diagnosis",
        }[name]
        await query.message.reply_text(prompts[name], reply_markup=_keyboard())
    elif name == "finish":
        await _finish(query, context)
    elif name == "exit":
        context.user_data.pop("mode", None)
        context.user_data.pop("virtual_patient_session", None)
        context.user_data.pop("vp_pending", None)
        await query.edit_message_text("تم إنهاء الحالة ✅")


def register(app):
    app.add_handler(CommandHandler("virtual_patient", cmd_virtual_patient))
    app.add_handler(CommandHandler("patient", cmd_virtual_patient))
    app.add_handler(
        MessageHandler(filters.Regex(r"^🩺\s*مريض افتراضي$"), button_virtual_patient),
        group=-8,
    )
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^🩺\s*المريض الافتراضي\s+تشغيل/إيقاف$"),
            button_toggle_virtual_patient,
        ),
        group=-9,
    )
    app.add_handler(CallbackQueryHandler(_callback, pattern=r"^vp:"))
