"""
Hikmat public API — the bridge between the game (student UI) and Frappe.

The game stays exactly as-is; it just fetches get_courses() instead of using a
hardcoded COURSES array, and posts submit_attempt() when a lesson activity ends.
Output of get_courses() matches the game's COURSES shape 1:1.
"""
import json

import frappe


@frappe.whitelist(allow_guest=True)
def get_courses():
    """Return the full curriculum as the game's COURSES array."""
    out = []

    published = frappe.get_all(
        "Track", filters={"published": 1},
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi"],
        order_by="sort_order asc, creation asc",
    )
    for t in published:
        out.append(_track_json(t, with_content=True))

    # locked / coming-soon tracks (shown greyed in the game, no lessons)
    locked = frappe.get_all(
        "Track", filters={"published": 0},
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi"],
        order_by="sort_order asc, creation asc",
    )
    for t in locked:
        out.append(_track_json(t, with_content=False))

    return out


def _track_json(t, with_content):
    track = {
        "key": t.track_key, "title": t.title, "titleHi": t.title_hi,
        "icon": t.icon, "color": t.color, "blurb": t.blurb, "blurbHi": t.blurb_hi,
        "published": bool(with_content), "lessons": [],
    }
    if not with_content:
        return track

    lessons = frappe.get_all(
        "Lesson", filters={"track": t.name, "published": 1},
        fields=["name", "lesson_key", "title", "title_hi"],
        order_by="sort_order asc, creation asc",
    )
    for l in lessons:
        words = []
        for w in frappe.get_all("Lesson Word", filters={"parent": l.name},
                                fields=["en", "hi", "pron", "emoji", "word_type", "uncountable", "plural", "use_en", "use_hi"],
                                order_by="idx asc"):
            word = {"en": w.en, "hi": w.hi, "pron": w.pron, "emoji": w.emoji}
            if w.word_type:
                word["type"] = w.word_type
            if w.use_en:
                word["use"] = w.use_en
                word["useHi"] = w.use_hi or ""
            if w.uncountable:
                word["uncount"] = True
            else:
                word["plural"] = w.plural or (w.en + "s")
            words.append(word)

        dialogues = []
        for d in frappe.get_all("Dialogue", filters={"lesson": l.name},
                                fields=["name", "who", "line", "line_hi", "followup"],
                                order_by="sort_order asc, creation asc"):
            replies = [{"text": r.text, "textHi": r.text_hi or "", "ok": bool(r.is_correct)}
                       for r in frappe.get_all("Dialogue Reply", filters={"parent": d.name},
                                               fields=["text", "text_hi", "is_correct"], order_by="idx asc")]
            dialogues.append({"who": d.who or "🙂", "line": d.line, "lineHi": d.line_hi,
                              "then": d.followup, "replies": replies})

        code = []
        for c in frappe.get_all("Lesson Code", filters={"parent": l.name},
                                fields=["prompt", "prompt_hi", "teach", "teach_hi", "code", "choices", "answer"],
                                order_by="idx asc"):
            code.append({
                "prompt": c.prompt, "promptHi": c.prompt_hi,
                "teach": c.teach or "", "teachHi": c.teach_hi or "",
                "lines": (c.code or "").split("\n"),
                "choices": [x for x in (c.choices or "").split("\n") if x != ""],
                "answer": c.answer,
            })

        fix = []
        for x in frappe.get_all("Lesson Fix", filters={"parent": l.name},
                                fields=["sentence", "wrong_word", "correction", "teach", "teach_hi"],
                                order_by="idx asc"):
            fix.append({
                "sentence": x.sentence, "wrongWord": x.wrong_word, "fix": x.correction,
                "teach": x.teach or "", "teachHi": x.teach_hi or "",
            })

        email = []
        for e in frappe.get_all("Lesson Email", filters={"parent": l.name},
                                fields=["scenario", "scenario_hi", "spec_json"],
                                order_by="idx asc"):
            try:
                spec = json.loads(e.spec_json or "{}")
            except Exception:
                spec = {}
            email.append({
                "scenario": e.scenario, "scenarioHi": e.scenario_hi or "",
                "to": spec.get("to", ""), "from": spec.get("from", ""),
                "slots": spec.get("slots", []),
            })

        track["lessons"].append({
            "key": l.lesson_key, "title": l.title, "titleHi": l.title_hi,
            "words": words, "dialogues": dialogues, "code": code, "fix": fix, "email": email,
        })
    return track


@frappe.whitelist(allow_guest=True)
def get_settings():
    s = frappe.get_single("Hikmat Settings")
    return {
        "appName": s.app_name or "Hikmat",
        "logo": s.logo or "",
        "taglineEn": s.tagline_en or "Learn English by playing",
        "taglineHi": s.tagline_hi or "",
        "defaultLanguage": s.default_language or "en",
        "helpDefaultOn": bool(s.help_default_on),
    }


@frappe.whitelist(allow_guest=True)
def submit_attempt(student=None, track=None, lesson=None, activity=None,
                   stars=0, score=0, total=0, coins=0):
    """Record one finished activity. Called by the game on the result screen."""
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort"], as_dict=True) or {}
    doc = frappe.get_doc({
        "doctype": "Lesson Attempt",
        "student": student, "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
        "track": track, "lesson": lesson, "activity": activity,
        "stars": int(stars or 0), "score": int(score or 0),
        "total": int(total or 0), "coins": int(coins or 0),
        "attempted_on": frappe.utils.now(),
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


# ---------------------------------------------------------------------------
# Student login (facilitator-managed; no email/password — pick your name)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_cohorts():
    return frappe.get_all("Cohort", fields=["name", "cohort_name", "center"],
                          order_by="cohort_name asc")


@frappe.whitelist(allow_guest=True)
def get_students(cohort=None):
    filters = {"active": 1}
    if cohort:
        filters["cohort"] = cohort
    rows = frappe.get_all("Student", filters=filters,
                          fields=["name", "student_name", "avatar", "login_pin"],
                          order_by="student_name asc")
    # never expose the PIN itself — only whether one is set
    return [{"id": r.name, "name": r.student_name, "avatar": r.avatar or "🙂",
             "hasPin": bool(r.login_pin)} for r in rows]


@frappe.whitelist(allow_guest=True)
def login_student(student, pin=None):
    s = frappe.db.get_value("Student", student,
                            ["student_name", "login_pin", "active", "avatar"], as_dict=True)
    if not s or not s.active:
        return {"ok": False, "error": "not_found"}
    if s.login_pin and str(s.login_pin) != str(pin or ""):
        return {"ok": False, "error": "wrong_pin"}
    return {"ok": True, "id": student, "name": s.student_name, "avatar": s.avatar or "🙂"}


@frappe.whitelist()
def active_student_count():
    """Distinct students who have at least one attempt (for the analytics card)."""
    r = frappe.db.sql("select count(distinct student) from `tabLesson Attempt`")
    return r[0][0] if r else 0


@frappe.whitelist()
def average_stars():
    """Avg stars across attempts, to 2 decimals (Int-field averaging mis-formats as currency)."""
    r = frappe.db.sql("select avg(stars) from `tabLesson Attempt`")
    return round(r[0][0], 2) if r and r[0][0] is not None else 0


@frappe.whitelist(allow_guest=True)
def get_progress(student):
    """Best stars per track/lesson/activity, from this student's attempts —
    so progress follows the girl across shared laptops."""
    rows = frappe.get_all("Lesson Attempt", filters={"student": student},
                          fields=["track", "lesson", "activity", "stars"])
    prog = {}
    for r in rows:
        prog.setdefault(r.track, {}).setdefault(r.lesson, {})
        if (r.stars or 0) > prog[r.track][r.lesson].get(r.activity, 0):
            prog[r.track][r.lesson][r.activity] = r.stars or 0
    return {"progress": prog}
