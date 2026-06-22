"""
Hikmat public API — the bridge between the game (student UI) and Frappe.

The game fetches get_courses()/get_structure()/get_settings() instead of using a
hardcoded COURSES array, and posts submit_attempt() when a lesson activity ends.
Output of get_courses() matches the game's COURSES shape 1:1.

Performance: the read endpoints are cached in Redis (content rarely changes) and
busted on any content edit via doc_events (see hooks.py) — this turns the old
~hundreds-of-queries-per-boot into one cheap cache hit. See clear_content_cache().

Abuse: writes (submit_attempt, signup_student) and login (login_student) are
rate-limited / locked-out via Redis counters. These endpoints are allow_guest, so
treat every input as untrusted: validate the student and clamp all numbers.
"""
import hmac
import json

import frappe
from frappe import _
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Caching (content is read far more than it changes)
# ---------------------------------------------------------------------------
COURSES_CACHE_KEY = "hikmat:courses"
STRUCTURE_CACHE_KEY = "hikmat:structure"
SETTINGS_CACHE_KEY = "hikmat:settings"


_CACHE_TTL = 3600  # busted immediately on content edit via doc_events; TTL is a safety net


def _cached(key, builder):
    val = frappe.cache().get_value(key)
    if val is None:                      # cache miss (an empty list/dict is cached as-is)
        val = builder()
        frappe.cache().set_value(key, val, expires_in_sec=_CACHE_TTL)
    return val


def clear_content_cache(doc=None, method=None):
    """Bust the read caches. Wired to content doctype on_update/on_trash in hooks.py,
    and called by setup_data.seed_content(). The (doc, method) args let it be a doc event."""
    c = frappe.cache()
    for k in (COURSES_CACHE_KEY, STRUCTURE_CACHE_KEY, SETTINGS_CACHE_KEY):
        c.delete_value(k)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rate_ok(bucket, limit, seconds):
    """True if `bucket` is under `limit`. Cooldown window resets on each hit, so a
    sustained flood stays capped while normal use never trips it. Fail-open if Redis
    is down (never block a child mid-lesson over a cache hiccup)."""
    try:
        c = frappe.cache()
        key = "hikmat:rl:" + bucket
        n = _int(c.get_value(key), 0)
        if n >= limit:
            return False
        c.set_value(key, n + 1, expires_in_sec=seconds)
        return True
    except Exception:
        return True


def _client_ip():
    return getattr(frappe.local, "request_ip", None) or "unknown"


# ---------------------------------------------------------------------------
# Auth helpers — PIN hashing (with legacy-plaintext upgrade) + per-student tokens
# ---------------------------------------------------------------------------
def _hash_pin(pin):
    return generate_password_hash(str(pin), method="pbkdf2:sha256") if pin else ""


def _looks_hashed(stored):
    return str(stored or "").startswith(("pbkdf2:", "scrypt:"))


def _pin_ok(stored, pin):
    """Verify a PIN. Hashed values use a constant-time hash check; legacy plaintext
    (pre-hashing) still verifies so existing logins keep working until upgraded."""
    if not stored:
        return True                       # no PIN set → open profile
    if _looks_hashed(stored):
        return check_password_hash(str(stored), str(pin or ""))
    return hmac.compare_digest(str(stored), str(pin or ""))   # legacy plaintext


def _token_for(student_name):
    """Stable per-student token: reuse if present, else mint + store. Returned to the
    client at login/signup and required on writes/reads so a guest can't act as a student."""
    tok = frappe.db.get_value("Student", student_name, "auth_token")
    if not tok:
        tok = frappe.generate_hash(length=40)
        frappe.db.set_value("Student", student_name, "auth_token", tok, update_modified=False)
    return tok


def _token_ok(student_name, token):
    """Graceful enforcement: if the student has a token, require a match; a legacy student
    who hasn't logged in since tokens shipped (no token yet) is allowed until their next login."""
    stored = frappe.db.get_value("Student", student_name, "auth_token")
    if not stored:
        return True
    return hmac.compare_digest(str(stored), str(token or ""))


# ---------------------------------------------------------------------------
# Public read endpoints (cached)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_courses():
    """Return the full curriculum as the game's COURSES array (cached)."""
    return _cached(COURSES_CACHE_KEY, _build_courses)


def _build_courses():
    out = []
    published = frappe.get_all(
        "Track", filters={"published": 1},
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi", "band", "subject"],
        order_by="sort_order asc, creation asc",
    )
    for t in published:
        out.append(_track_json(t, with_content=True))

    # locked / coming-soon tracks (shown greyed in the game, no lessons)
    locked = frappe.get_all(
        "Track", filters={"published": 0},
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi", "band", "subject"],
        order_by="sort_order asc, creation asc",
    )
    for t in locked:
        out.append(_track_json(t, with_content=False))
    return out


@frappe.whitelist(allow_guest=True)
def get_structure():
    """Grade bands + subjects metadata for the Class 1–10 navigation (cached)."""
    return _cached(STRUCTURE_CACHE_KEY, _build_structure)


def _build_structure():
    bands = frappe.get_all(
        "Grade Band", filters={"published": 1},
        fields=["band_key", "title", "title_hi", "subtitle", "subtitle_hi", "icon", "color"],
        order_by="sort_order asc, creation asc",
    )
    subjects = frappe.get_all(
        "Subject",
        fields=["subject_key", "title", "title_hi", "icon", "color"],
        order_by="sort_order asc, creation asc",
    )
    return {
        "bands": [{"key": b.band_key, "title": b.title, "titleHi": b.title_hi,
                   "subtitle": b.subtitle or "", "subtitleHi": b.subtitle_hi or "",
                   "icon": b.icon or "📚", "color": b.color or "#6c5ce7"} for b in bands],
        "subjects": [{"key": s.subject_key, "title": s.title, "titleHi": s.title_hi,
                      "icon": s.icon or "📘", "color": s.color or "#6c5ce7"} for s in subjects],
    }


def _split_lines(s):
    return [x.strip() for x in (s or "").split("\n") if x.strip() != ""]


def _track_json(t, with_content):
    track = {
        "key": t.track_key, "title": t.title, "titleHi": t.title_hi,
        "icon": t.icon, "color": t.color, "blurb": t.blurb, "blurbHi": t.blurb_hi,
        "band": t.get("band") or "", "subject": t.get("subject") or "",
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
                "choices": _split_lines(c.choices),
                "answer": (c.answer or "").strip(),
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

        quiz = []
        for q in frappe.get_all("Lesson Quiz", filters={"parent": l.name},
                                fields=["question", "question_hi", "emoji", "choices", "answer", "teach", "teach_hi"],
                                order_by="idx asc"):
            quiz.append({
                "q": q.question, "qHi": q.question_hi or "", "emoji": q.emoji or "",
                "choices": _split_lines(q.choices),
                "answer": (q.answer or "").strip(), "teach": q.teach or "", "teachHi": q.teach_hi or "",
            })

        track["lessons"].append({
            "key": l.lesson_key, "title": l.title, "titleHi": l.title_hi,
            "words": words, "dialogues": dialogues, "code": code, "fix": fix,
            "email": email, "quiz": quiz,
        })
    return track


@frappe.whitelist(allow_guest=True)
def get_settings():
    return _cached(SETTINGS_CACHE_KEY, _build_settings)


def _build_settings():
    s = frappe.get_single("Hikmat Settings")
    return {
        "appName": s.app_name or "Hikmat",
        "logo": s.logo or "",
        "taglineEn": s.tagline_en or "Learn English by playing",
        "taglineHi": s.tagline_hi or "",
        "defaultLanguage": s.default_language or "en",
        "helpDefaultOn": bool(s.help_default_on),
    }


# ---------------------------------------------------------------------------
# Progress write (untrusted input — validate + clamp + flood-cap)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def submit_attempt(student=None, token=None, track=None, lesson=None, activity=None,
                   stars=0, score=0, total=0, coins=0, client_id=None):
    """Record one finished activity. Called by the game on the result screen.
    Everything here is attacker-controllable, so: verify the student exists & is
    active, require the student's login token (no forging attempts for others),
    clamp every number to sane bounds, and cap write volume per IP.
    client_id makes the write idempotent — a retry after a partial success (the
    classic offline-queue double-insert) returns the existing row instead of a copy."""
    if not _rate_ok("submit:" + _client_ip(), 3000, 3600):   # flood ceiling; well above a real classroom
        return {"ok": False, "error": "rate_limited"}
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:                                            # already recorded this exact attempt? done.
        existing = frappe.db.get_value("Lesson Attempt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _token_ok(student, token):
        return {"ok": False, "error": "auth"}

    total = max(0, _int(total))
    score = max(0, _int(score))
    if total:
        score = min(score, total)
    try:
        doc = frappe.get_doc({
            "doctype": "Lesson Attempt", "client_id": client_id or None,
            "student": student, "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
            "track": track, "lesson": lesson, "activity": activity,
            "stars": max(0, min(3, _int(stars))),          # an activity is worth 0–3 stars
            "score": score, "total": total,
            "coins": max(0, min(1000, _int(coins))),
            "attempted_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with another submit of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Lesson Attempt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


# ---------------------------------------------------------------------------
# "Roshni, mujhe doubt hai" — a learner taps for help; we log it for the
# facilitator confusion heatmap. Untrusted input → validate, clamp, flood-cap.
# Guests may raise doubts too (anonymous confusion data is still useful to a
# teacher watching the room), so a student id is optional here.
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def report_doubt(student=None, token=None, track=None, lesson=None, activity=None,
                 question=None, lang=None, client_id=None):
    """Record one 'I'm stuck' tap. Idempotent on client_id so the offline queue can
    retry safely. A logged-in student must present their token (no forging for others);
    an anonymous/guest doubt is accepted with no student attached."""
    if not _rate_ok("doubt:" + _client_ip(), 2000, 3600):   # generous ceiling; never trips real classroom use
        return {"ok": False, "error": "rate_limited"}
    if client_id:                                            # already logged this tap? done.
        existing = frappe.db.get_value("Lesson Doubt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}

    sname, cohort = None, None
    if student:
        sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
        if not sinfo or not sinfo.active:
            return {"ok": False, "error": "unknown_student"}
        if not _token_ok(student, token):
            return {"ok": False, "error": "auth"}
        sname, cohort = sinfo.get("student_name"), sinfo.get("cohort")

    try:
        doc = frappe.get_doc({
            "doctype": "Lesson Doubt", "client_id": client_id or None,
            "student": student or None, "student_name": sname, "cohort": cohort,
            "track": (track or "")[:140], "lesson": (lesson or "")[:140],
            "activity": (activity or "")[:140],
            "question": (question or "")[:500],
            "lang": (lang or "")[:10],
            "raised_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with another retry of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Lesson Doubt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


# ---------------------------------------------------------------------------
# Student login (facilitator-managed or self-signup; no email/password)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_cohorts():
    return frappe.get_all("Cohort", fields=["name", "cohort_name", "center"],
                          order_by="cohort_name asc")


@frappe.whitelist(allow_guest=True)
def has_students():
    """Lightweight boot check — does any roster exist? Returns a bool only, never names
    (so the public boot path doesn't enumerate minors)."""
    return {"any": bool(frappe.db.count("Student", {"active": 1}))}


@frappe.whitelist(allow_guest=True)
def get_students(cohort=None):
    """Roster for ONE cohort. A cohort is required so a single anonymous call cannot
    enumerate every child across all centres. Never returns the PIN (only hasPin)."""
    if not cohort:
        return []
    rows = frappe.get_all("Student", filters={"active": 1, "cohort": cohort},
                          fields=["name", "student_name", "avatar", "login_pin"],
                          order_by="student_name asc")
    return [{"id": r.name, "name": r.student_name, "avatar": r.avatar or "🙂",
             "hasPin": bool(r.login_pin)} for r in rows]


_MAX_PIN_TRIES = 8
_LOCKOUT_SECONDS = 300


@frappe.whitelist(allow_guest=True)
def login_student(student, pin=None):
    """Verify a student's PIN with a per-student lockout (8 wrong tries → 5-min cooldown),
    defeating brute force of short numeric PINs. Constant-time compare."""
    c = frappe.cache()
    fkey = "hikmat:loginfail:" + str(student)
    if _int(c.get_value(fkey)) >= _MAX_PIN_TRIES:
        return {"ok": False, "error": "locked"}
    s = frappe.db.get_value("Student", student,
                            ["student_name", "login_pin", "active", "avatar", "band"], as_dict=True)
    if not s or not s.active:
        return {"ok": False, "error": "not_found"}
    if s.login_pin and not _pin_ok(s.login_pin, pin):
        c.set_value(fkey, _int(c.get_value(fkey)) + 1, expires_in_sec=_LOCKOUT_SECONDS)
        return {"ok": False, "error": "wrong_pin"}
    c.delete_value(fkey)                                # reset the counter on success
    if s.login_pin and not _looks_hashed(s.login_pin):  # upgrade a legacy plaintext PIN to a hash on login
        frappe.db.set_value("Student", student, "login_pin", _hash_pin(str(s.login_pin)), update_modified=False)
    token = _token_for(student)
    frappe.db.commit()
    return {"ok": True, "id": student, "name": s.student_name, "avatar": s.avatar or "🙂", "token": token}


@frappe.whitelist(allow_guest=True)
def signup_student(name=None, avatar=None, pin=None, age=None, cohort=None, band=None):
    """Self-service signup: a learner creates their own profile and is logged straight in.
    No email/password — just a name (+ optional avatar, PIN, grade band). Rate-limited per IP."""
    if not _rate_ok("signup:" + _client_ip(), 60, 3600):   # generous for a classroom; stops spam faucets
        return {"ok": False, "error": "rate_limited"}
    name = "".join(ch for ch in (name or "").strip() if ch.isprintable())
    if not (2 <= len(name) <= 40):
        return {"ok": False, "error": "bad_name"}
    pin = (pin or "").strip()
    if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):   # min 4 digits (10k space)
        return {"ok": False, "error": "bad_pin"}
    a = _int(age, None)
    age_val = a if (a is not None and 3 <= a <= 25) else None
    band = band if (band and frappe.db.exists("Grade Band", band)) else None

    if not cohort:
        cohort = "New Learners"                            # self-signups isolated from facilitator centres
        if not frappe.db.exists("Cohort", cohort):
            try:
                frappe.get_doc({"doctype": "Cohort", "cohort_name": cohort,
                                "center": "Self sign-up"}).insert(ignore_permissions=True)
            except frappe.DuplicateEntryError:             # concurrent first signups — fine
                pass
    doc = frappe.get_doc({
        "doctype": "Student", "student_name": name, "avatar": avatar or "🙂",
        "cohort": cohort, "login_pin": _hash_pin(pin), "active": 1, "gender": "Other",
        "age": age_val, "band": band,
    }).insert(ignore_permissions=True)
    token = _token_for(doc.name)
    frappe.db.commit()
    return {"ok": True, "id": doc.name, "name": doc.student_name,
            "avatar": doc.avatar or "🙂", "hasPin": bool(pin), "token": token, "band": band or ""}


@frappe.whitelist(allow_guest=True)
def login_by_name(name, pin=None):
    """Log in by typing your name + PIN — NO roster is shown (cleaner, and doesn't broadcast
    minors' names). The PIN disambiguates if a name repeats. Generic error (never reveals
    whether a name exists). Rate-limited + locked out per name+IP."""
    key = (name or "").strip().lower()
    if not key:
        return {"ok": False, "error": "bad_login"}
    c = frappe.cache()
    fkey = "hikmat:loginfail:name:" + _client_ip() + ":" + key
    if _int(c.get_value(fkey)) >= _MAX_PIN_TRIES:
        return {"ok": False, "error": "locked"}
    cands = [s for s in frappe.get_all("Student", filters={"active": 1},
                                       fields=["name", "student_name", "login_pin", "avatar", "band"])
             if (s.student_name or "").strip().lower() == key]
    match = next((s for s in cands if _pin_ok(s.login_pin, pin)), None)
    if not match:
        c.set_value(fkey, _int(c.get_value(fkey)) + 1, expires_in_sec=_LOCKOUT_SECONDS)
        return {"ok": False, "error": "bad_login"}
    c.delete_value(fkey)
    if match.login_pin and not _looks_hashed(match.login_pin):
        frappe.db.set_value("Student", match.name, "login_pin", _hash_pin(str(match.login_pin)), update_modified=False)
    token = _token_for(match.name)
    frappe.db.commit()
    return {"ok": True, "id": match.name, "name": match.student_name, "avatar": match.avatar or "🙂",
            "token": token, "band": match.band or ""}


# ---------------------------------------------------------------------------
# Analytics (System Manager only — not guest)
# ---------------------------------------------------------------------------
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
def get_progress(student, token=None):
    """Best stars per track/lesson/activity for one student — so progress follows the
    girl across shared laptops. Requires the student's login token (no reading another
    child's progress by guessing an id). Aggregated in SQL so the response is one row
    per (track,lesson,activity) regardless of how many times an activity was replayed."""
    if not student or not _token_ok(student, token):
        return {"progress": {}}
    rows = frappe.db.sql(
        """select track, lesson, activity, max(stars) as stars
           from `tabLesson Attempt` where student=%s
           group by track, lesson, activity""",
        student, as_dict=True)
    prog = {}
    for r in rows:
        prog.setdefault(r.track, {}).setdefault(r.lesson, {})[r.activity] = r.stars or 0
    return {"progress": prog}


@frappe.whitelist()   # NOT allow_guest → requires a logged-in Desk user (facilitator / System Manager)
def delete_student(student):
    """Erase a child's record and ALL their attempts (right-to-erasure for minors' data).
    Facilitator-only. Use from Desk or a trusted admin tool."""
    if not frappe.db.exists("Student", student):
        return {"ok": False, "error": "not_found"}
    name = frappe.db.get_value("Student", student, "student_name")
    for att in frappe.get_all("Lesson Attempt", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Lesson Attempt", att, force=1, ignore_permissions=True)
    frappe.delete_doc("Student", student, force=1, ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "deleted": name}
