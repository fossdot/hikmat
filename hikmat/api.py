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
import re

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
    """Verify a PIN, FAIL-CLOSED. A profile with no PIN cannot be authenticated — this
    closes the shared-laptop hole where a PIN-less profile opened with zero auth. Hashed
    values use a constant-time hash check; legacy plaintext (pre-hashing) still verifies
    so existing logins keep working until upgraded on next successful login."""
    if not stored or not pin:
        return False                      # no PIN set (or none supplied) → cannot authenticate
    if _looks_hashed(stored):
        return check_password_hash(str(stored), str(pin))
    return hmac.compare_digest(str(stored), str(pin))         # legacy plaintext


_TOKEN_TTL_DAYS = 90


def _token_valid(issued_on):
    """A token is live for _TOKEN_TTL_DAYS since it was last issued/refreshed."""
    if not issued_on:
        return False
    return frappe.utils.time_diff_in_seconds(frappe.utils.now(), issued_on) <= _TOKEN_TTL_DAYS * 86400


def _token_for(student_name):
    """Return a live token for the student, called on every successful login/signup.
    Sliding-window expiry: a still-valid token keeps its VALUE (so a girl stays logged in
    across the shared laptops she's used) but its issued-on slides forward, so an actively
    used account never expires. A missing or expired token is rotated to a fresh value."""
    row = frappe.db.get_value("Student", student_name, ["auth_token", "token_issued_on"], as_dict=True)
    tok = row.auth_token if row else None
    if not tok or not _token_valid(row.token_issued_on):
        tok = frappe.generate_hash(length=40)                # mint / rotate
    frappe.db.set_value("Student", student_name,
                        {"auth_token": tok, "token_issued_on": frappe.utils.now()},
                        update_modified=False)
    return tok


def _token_ok(student_name, token):
    """Validate a bearer token, FAIL-CLOSED: a student with no token, an expired token, or a
    mismatch is rejected. (Legacy token-less students simply re-login, which mints one.)"""
    row = frappe.db.get_value("Student", student_name, ["auth_token", "token_issued_on"], as_dict=True)
    if not row or not row.auth_token or not _token_valid(row.token_issued_on):
        return False
    return hmac.compare_digest(str(row.auth_token), str(token or ""))


# -- Dual auth: the game has two kinds of learner ------------------------------
# CAMPUS (offline-capable) students are custom Student docs authed by a per-student
# bearer token (_token_ok). ONLINE students are Frappe Website Users, so their request
# already carries a logged-in session — the linked Student is found via Student.user.
# A request is authorized for a student if EITHER proof holds.
def _session_student():
    """The Student linked to the currently logged-in (online) Website User, if any."""
    u = getattr(frappe.session, "user", None)
    if u and u != "Guest":
        return frappe.db.get_value("Student", {"user": u, "active": 1}, "name")
    return None


def _authorized(student, token):
    """True if the caller may act as `student`: a matching campus token, OR an online
    session whose linked Student is exactly this one."""
    if student and _token_ok(student, token):
        return True
    ss = _session_student()
    return bool(ss and ss == student)


# ---------------------------------------------------------------------------
# Facilitator notifications — surface a learner's "I'm stuck" tap (and, later,
# milestone checkpoints) to every facilitator's Desk bell. Best-effort: a
# notification failure must never block the child's action.
# ---------------------------------------------------------------------------
def _facilitator_users():
    """Enabled Desk users who facilitate — currently the System Managers, minus system
    accounts. (A dedicated 'Facilitator' role can be added here later.)"""
    users = frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"},
                           pluck="parent")
    return [u for u in set(users)
            if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled")]


def _notify_facilitators(subject, doctype=None, docname=None):
    """Drop a Desk Notification Log (bell alert) for every facilitator."""
    try:
        for u in _facilitator_users():
            frappe.get_doc({
                "doctype": "Notification Log", "for_user": u, "type": "Alert",
                "subject": (subject or "")[:140],
                "document_type": doctype or "", "document_name": docname or "",
            }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()   # doubt was already committed; only the notifications roll back


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
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi", "band", "subject",
                "video", "video_title", "video_title_hi", "video_duration_secs", "video_captions", "video_captions_hi"],
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

    # Explainer video (optional, streams online-only — the game shows a friendly
    # offline card when unreachable). Keys are simply absent when no video is set,
    # so old caches and the bundled fallback COURSES need no migration.
    if (t.get("video") or "").strip():
        track["videoUrl"] = t.video.strip()
        track["videoTitle"] = t.get("video_title") or ""
        track["videoTitleHi"] = t.get("video_title_hi") or ""
        if _int(t.get("video_duration_secs")):
            track["videoDuration"] = _int(t.get("video_duration_secs"))
        if (t.get("video_captions") or "").strip():
            track["videoCaptions"] = t.video_captions.strip()
        if (t.get("video_captions_hi") or "").strip():
            track["videoCaptionsHi"] = t.video_captions_hi.strip()

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

        read = []
        for r in frappe.get_all("Lesson Read", filters={"parent": l.name},
                                fields=["title", "title_hi", "emoji", "passage", "passage_hi",
                                        "question", "question_hi", "choices", "answer", "teach", "teach_hi"],
                                order_by="idx asc"):
            read.append({
                "title": r.title or "", "titleHi": r.title_hi or "", "emoji": r.emoji or "",
                "text": r.passage or "", "textHi": r.passage_hi or "",
                "q": r.question or "", "qHi": r.question_hi or "",
                "choices": _split_lines(r.choices), "answer": (r.answer or "").strip(),
                "teach": r.teach or "", "teachHi": r.teach_hi or "",
            })

        track["lessons"].append({
            "key": l.lesson_key, "title": l.title, "titleHi": l.title_hi,
            "words": words, "dialogues": dialogues, "code": code, "fix": fix,
            "email": email, "quiz": quiz, "read": read,
        })

    # Module test: the question bank ships WITH the curriculum so tests work fully
    # offline (answers therefore exist in the client payload — accepted tradeoff: the
    # audience is not dev-tools-savvy, and the anti-cheat targets the realistic threat
    # of switching apps to ask/look up, not payload inspection). teach/teach_hi are
    # deliberately NOT exported — no hints inside a test.
    mt = frappe.db.get_value("Module Test", {"track": t.name, "active": 1},
                             ["name", "questions_per_paper", "pass_pct", "time_limit_secs",
                              "intro", "intro_hi"], as_dict=True)
    if mt:
        bank = [{"id": q.name, "q": q.question, "qHi": q.question_hi or "",
                 "emoji": q.emoji or "", "choices": _split_lines(q.choices),
                 "answer": (q.answer or "").strip()}
                for q in frappe.get_all("Module Test Question", filters={"parent": mt.name},
                                        fields=["name", "question", "question_hi", "emoji",
                                                "choices", "answer"], order_by="idx asc")]
        if bank:
            track["test"] = {"questionsPerPaper": _int(mt.questions_per_paper) or 10,
                             "passPct": _int(mt.pass_pct) or 60,
                             "timeLimitSecs": _int(mt.time_limit_secs) or 600,
                             "intro": mt.intro or "", "introHi": mt.intro_hi or "",
                             "bank": bank}
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
        "defaultTheme": s.get("default_theme") or "light",
        "defaultSound": bool(s.get("default_sound")),
        # Only the on/off flags are public — the model, endpoints, system prompt and crisis
        # copy stay server-side (read from the Single inside ai_ask/ai_transcribe/ai_tts),
        # never in this cached payload that any guest can fetch.
        "aiEnabled": bool(s.get("ai_enabled")),
        "voiceEnabled": bool(s.get("voice_enabled")),
        # Belt thresholds ship with settings so gate DETECTION works fully offline;
        # CLEARING stays server-side (Evaluation status, synced via get_progress).
        "milestones": [{"key": m.milestone_key, "title": m.title, "titleHi": m.title_hi or "",
                        "icon": m.icon or "🏅", "threshold": m.threshold_gems}
                       for m in _active_milestones()],
    }


# ---------------------------------------------------------------------------
# Progress write (untrusted input — validate + clamp + flood-cap)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def submit_attempt(student=None, token=None, track=None, lesson=None, activity=None,
                   stars=0, score=0, total=0, coins=0, duration_secs=0, client_id=None):
    """Record one finished activity. Called by the game on the result screen.
    Everything here is attacker-controllable, so: verify the student exists & is
    active, require the student's login token (no forging attempts for others),
    clamp every number to sane bounds, and cap write volume per IP.
    client_id makes the write idempotent — a retry after a partial success (the
    classic offline-queue double-insert) returns the existing row instead of a copy."""
    if not _rate_ok("submit:" + _client_ip(), 3000, 3600):   # flood ceiling; well above a real classroom
        return {"ok": False, "error": "rate_limited"}
    if not student:
        student = _session_student()                         # online client authed by session, may omit id
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:                                            # already recorded this exact attempt? done.
        existing = frappe.db.get_value("Lesson Attempt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):                      # campus token OR online session
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
            "duration_secs": max(0, min(7200, _int(duration_secs))),   # 2h cap kills left-open-overnight noise
            "attempted_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with another submit of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Lesson Attempt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    gate = _check_milestones(student, sinfo)                 # belt threshold crossed? (never blocks the write)
    out = {"ok": True, "name": doc.name}
    if gate:
        out["milestone"] = gate
    return out


_TEST_STATUS = {"completed": "Completed", "exited": "Exited", "timed_out": "Timed Out"}


@frappe.whitelist(allow_guest=True)
def submit_test(student=None, token=None, track=None, paper=None, score=0, total=0,
                status=None, exit_reason=None, duration_secs=0, lang=None, client_id=None):
    """Record one module-test attempt (the mandatory end-of-track test). Same
    hardening as submit_attempt: rate cap, client_id idempotency, active-student +
    token check, clamps. Two rules are SERVER-enforced so the client can't soften
    them: an Exited (anti-cheat voided) attempt always scores 0, and pass/fail is
    recomputed here against the Module Test's pass_pct — a timed-out paper still
    counts what was answered (running out of time is not cheating)."""
    if not _rate_ok("testsub:" + _client_ip(), 600, 3600):   # tests are ~10× rarer than activities
        return {"ok": False, "error": "rate_limited"}
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:
        existing = frappe.db.get_value("Test Attempt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}

    st = _TEST_STATUS.get((status or "").strip().lower())
    if not st:
        return {"ok": False, "error": "bad_status"}
    total = max(0, _int(total))
    score = min(max(0, _int(score)), total)
    if st == "Exited":                                       # voiding is not client-optional
        score = 0
    exit_reason = (exit_reason or "")[:40] if st == "Exited" else ""
    try:
        ids = json.loads(paper or "[]")
        ids = [str(x)[:140] for x in ids[:100]] if isinstance(ids, list) else []
    except Exception:
        ids = []                                             # telemetry only — never reject the write

    pass_pct = 60
    track_doc = frappe.db.get_value("Track", {"track_key": (track or "")[:140]}, "name")
    if track_doc:
        pass_pct = _int(frappe.db.get_value("Module Test", {"track": track_doc}, "pass_pct")) or 60
    pct = round(100 * score / total) if total else 0
    passed = 1 if st in ("Completed", "Timed Out") and pct >= pass_pct else 0

    try:
        doc = frappe.get_doc({
            "doctype": "Test Attempt", "client_id": client_id or None,
            "student": student, "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
            "track": (track or "")[:140], "paper": json.dumps(ids),
            "score": score, "total": total, "pct": pct, "passed": passed,
            "status": st, "exit_reason": exit_reason,
            "duration_secs": max(0, min(7200, _int(duration_secs))),
            "lang": (lang or "")[:10],
            "attempted_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with a retry of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Test Attempt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    return {"ok": True, "name": doc.name, "passed": bool(passed), "pct": pct}


# ---------------------------------------------------------------------------
# Milestone "belt" gates — configurable star thresholds; crossing one creates a
# Pending Evaluation (an in-person facilitator rubric) and notifies facilitators.
# Clearing is server-authoritative: a facilitator marks the Evaluation Passed in
# Desk; the client syncs gate status down via get_progress.
# ---------------------------------------------------------------------------
def _active_milestones():
    """Active milestones, cheapest-first. Cached with the settings payload lifecycle."""
    return frappe.get_all("Hikmat Milestone", filters={"active": 1},
                          fields=["milestone_key", "title", "title_hi", "icon", "threshold_gems"],
                          order_by="threshold_gems asc")


def _total_gems(student):
    """A student's global gem total 💎 = SUM of coins over every attempt (score*5 +
    stars*10 each) — mirrors the client's state.coins, and unlike stars it keeps
    growing on replays, so practice counts toward the next belt."""
    r = frappe.db.sql(
        "select coalesce(sum(coins), 0) from `tabLesson Attempt` where student=%s", student)
    return int(r[0][0]) if r else 0


def _check_milestones(student, sinfo):
    """After a committed attempt: create a Pending Evaluation for every newly-crossed
    active milestone and ping the facilitators. Failures here must never undo the
    attempt (already committed), so everything is wrapped and rolled back on error.
    Returns the highest newly-crossed milestone key (for the client's celebration)."""
    try:
        milestones = _active_milestones()
        if not milestones:
            return None
        total = _total_gems(student)
        campus = frappe.db.get_value("Student", student, "campus")
        crossed = None
        for m in milestones:
            if total < (m.threshold_gems or 0):
                break                                        # sorted ascending — nothing further is reached
            if frappe.db.exists("Evaluation", {"student": student, "milestone": m.milestone_key}):
                continue                                     # already pending/passed — one row per belt, ever
            frappe.get_doc({
                "doctype": "Evaluation", "student": student,
                "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
                "campus": campus, "milestone": m.milestone_key,
                "threshold_gems": m.threshold_gems, "gems_at_reach": total,
                "status": "Pending", "reached_on": frappe.utils.now(),
            }).insert(ignore_permissions=True)
            frappe.db.commit()
            _notify_facilitators(
                "🏅 %s reached %s (%s💎) — evaluation needed" %
                (sinfo.get("student_name") or student, m.title, total),
                "Evaluation", "EV-%s-%s" % (student, m.milestone_key))
            crossed = m.milestone_key
        return crossed
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "hikmat milestone check")
        return None


def validate_cohort(doc, method=None):
    """doc_events hook: mandatory_depends_on only guards the Desk FORM — enforce the
    same rule server-side so an API/script insert can't create an undated Offline batch."""
    if (doc.mode or "Offline") == "Offline" and not doc.start_date:
        frappe.throw(_("An Offline cohort needs a start date."), frappe.MandatoryError)


def stamp_evaluation(doc, method=None):
    """doc_events hook: when a facilitator sets an outcome in Desk, stamp who/when."""
    if doc.status in ("Passed", "Needs Practice") and not doc.evaluated_on:
        doc.evaluated_by = frappe.session.user
        doc.evaluated_on = frappe.utils.now()


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
    if not student:
        student = _session_student()                         # online client may rely on its session
    if student:
        sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
        if not sinfo or not sinfo.active:
            return {"ok": False, "error": "unknown_student"}
        if not _authorized(student, token):                  # campus token OR online session
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
    # Ping the facilitators' Desk so "I'm stuck" reaches a human (AI tutor is deferred).
    who = sname or _("a learner")
    _notify_facilitators(_("🙋 Doubt from {0}: {1}").format(who, (question or "")[:80]),
                         "Lesson Doubt", doc.name)
    return {"ok": True, "name": doc.name}


@frappe.whitelist(allow_guest=True)
def log_event(student=None, token=None, kind=None, track=None, lesson=None, activity=None,
              question=None, chosen=None, answer=None, lang=None, client_id=None,
              tool=None, duration_secs=None, count=None):
    """Record one fine-grained learning event. Mirrors report_doubt: idempotent on
    client_id, offline-queue friendly, guests allowed (anonymous data still tells the
    teacher which QUESTION or ACTIVITY is broken). No notification — this is a
    high-volume analytics stream, not an alert. Kinds:
      wrong_answer — the exact question a learner missed and what she picked instead
      dwell        — time spent on an activity she LEFT without finishing (finished
                     time rides on Lesson Attempt.duration_secs); duration_secs
      tool_use     — batched taps of a UI tool (listen / lang_switch / replay …);
                     tool + count, aggregated client-side per activity
      test_exit    — a module test was voided by the anti-cheat guard; tool carries
                     the reason (hidden/blur/fullscreen_exit/pagehide/stopped),
                     duration_secs = seconds into the test, count = question reached"""
    if not _rate_ok("event:" + _client_ip(), 6000, 3600):   # wrong answers come in bursts; keep the ceiling high
        return {"ok": False, "error": "rate_limited"}
    if kind not in ("wrong_answer", "dwell", "tool_use", "test_exit"):
        return {"ok": False, "error": "bad_kind"}
    duration_secs = max(0, min(7200, _int(duration_secs)))  # same 2h sanity cap as attempts
    count = max(1, min(1000, _int(count) or 1))
    if kind == "dwell" and duration_secs <= 0:
        return {"ok": False, "error": "bad_duration"}
    if kind == "tool_use" and not (tool or "").strip():
        return {"ok": False, "error": "bad_tool"}
    if client_id:
        existing = frappe.db.get_value("Learning Event", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}

    sname, cohort = None, None
    if not student:
        student = _session_student()
    if student:
        sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
        if not sinfo or not sinfo.active:
            return {"ok": False, "error": "unknown_student"}
        if not _authorized(student, token):
            return {"ok": False, "error": "auth"}
        sname, cohort = sinfo.get("student_name"), sinfo.get("cohort")

    try:
        doc = frappe.get_doc({
            "doctype": "Learning Event", "client_id": client_id or None,
            "student": student or None, "student_name": sname, "cohort": cohort,
            "kind": kind,
            "track": (track or "")[:140], "lesson": (lesson or "")[:140],
            "activity": (activity or "")[:140],
            "tool": (tool or "")[:40],
            "duration_secs": duration_secs, "count": count,
            "question": (question or "")[:140],
            "chosen": (chosen or "")[:140], "answer": (answer or "")[:140],
            "lang": (lang or "")[:10],
            "occurred_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing = frappe.db.get_value("Learning Event", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


# ---------------------------------------------------------------------------
# Attendance — the game banks a logged-in student's ACTIVE screen time on-device
# and flushes it here in small deltas (offline-queued, idempotent). Two tiers:
# Attendance Ping is the raw audit log (client_id-deduped), Attendance Day is the
# per-(student, local-date) aggregate the facilitator reports read. Facilitator
# report only — nothing about attendance is ever shown to students.
# ---------------------------------------------------------------------------
_ATT_PING_MAX_SECS = 900        # one ping can never claim more than 15 minutes
_ATT_PAST_WINDOW_DAYS = 30      # matches the client's day store — a campus laptop
                                # that stays offline for weeks must not lose real
                                # attendance when it finally syncs
_ATT_FUTURE_WINDOW_DAYS = 1     # tolerate a device clock slightly ahead


@frappe.whitelist(allow_guest=True)
def log_attendance(student=None, token=None, date=None, secs=0, client_id=None, device_id=None):
    """Record one active-time delta. The client's LOCAL date is the day-of-record
    (campus devices are offline; server date would misfile late-night syncs), and
    received_on keeps the server-side audit anchor. The 900s per-ping cap means
    forging a Present day (>=150 min) takes 10+ pings with unique client_ids —
    visible in the audit trail — rather than one big number."""
    if not _rate_ok("att:" + _client_ip(), 2000, 3600):     # a 30-laptop room ≈ 360/hr
        return {"ok": False, "error": "rate_limited"}
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:
        existing = frappe.db.get_value("Attendance Ping", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "dedup": True}
    sinfo = frappe.db.get_value("Student", student,
                                ["student_name", "cohort", "campus", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}

    try:
        d = frappe.utils.getdate(date)
    except Exception:
        return {"ok": False, "error": "bad_date"}
    today = frappe.utils.getdate()
    if (today - d).days > _ATT_PAST_WINDOW_DAYS or (d - today).days > _ATT_FUTURE_WINDOW_DAYS:
        return {"ok": False, "error": "date_out_of_range"}
    secs = max(0, min(_ATT_PING_MAX_SECS, _int(secs)))
    if secs <= 0:
        return {"ok": False, "error": "bad_secs"}

    # Upsert the Day aggregate FIRST, then insert the Ping. If the ping insert races
    # a duplicate client_id, the rollback undoes the day increment too — so the
    # dedup ledger (pings) and the aggregate can never drift apart. (Inserting the
    # ping first would let a raced Day insert roll the ping away while keeping the
    # seconds — the classic double-count on retry.)
    min_minutes = _int(frappe.db.get_single_value("Hikmat Settings", "attendance_min_minutes")) or 150
    now = frappe.utils.now()
    for _attempt in range(2):
        day_name = frappe.db.get_value("Attendance Day", {"student": student, "date": d}, "name")
        try:
            if day_name:
                frappe.db.sql(
                    """update `tabAttendance Day`
                       set active_secs = active_secs + %s, last_ping = %s,
                           present = (active_secs >= %s)
                       where name = %s""",
                    (secs, now, min_minutes * 60, day_name))
            else:
                frappe.get_doc({
                    "doctype": "Attendance Day", "student": student,
                    "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
                    "campus": sinfo.get("campus"), "date": d,
                    "active_secs": secs, "present": 1 if secs >= min_minutes * 60 else 0,
                    "device_count": 1, "first_ping": now, "last_ping": now,
                }).insert(ignore_permissions=True)
            frappe.get_doc({
                "doctype": "Attendance Ping", "client_id": client_id or None,
                "student": student, "student_name": sinfo.get("student_name"),
                "date": d, "secs": secs, "device_id": (device_id or "")[:60],
                "received_on": now,
            }).insert(ignore_permissions=True)
            break
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            # Either the same client_id landed twice (→ dedup, done) or two devices
            # raced the first Day insert (→ retry once; the update path now wins).
            if client_id and frappe.db.get_value("Attendance Ping", {"client_id": client_id}, "name"):
                return {"ok": True, "dedup": True}
    else:
        return {"ok": False, "error": "conflict"}

    # device_count from the audit trail (cheap: one day's pings for one student)
    total, devices = frappe.db.sql(
        """select coalesce(sum(secs), 0), count(distinct device_id)
           from `tabAttendance Ping` where student=%s and date=%s""", (student, d))[0]
    frappe.db.sql("update `tabAttendance Day` set device_count=%s where student=%s and date=%s",
                  (max(1, _int(devices)), student, d))
    frappe.db.commit()
    return {"ok": True, "secs_today": _int(total), "present": _int(total) >= min_minutes * 60}


def prune_attendance_pings():
    """Daily housekeeping (hooks.py scheduler): raw pings older than 90 days are only
    needed for client_id dedup and short-term audit — the client's own queue/day-store
    horizon is 30 days, so a 90-day retention can never re-admit a replayed ping."""
    try:
        frappe.db.sql("delete from `tabAttendance Ping` where received_on < %s",
                      frappe.utils.add_days(frappe.utils.now(), -90))
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "prune_attendance_pings")


# ---------------------------------------------------------------------------
# "Roshni AI" — the local-Ollama voice/text tutor. The game posts a child's typed
# (later: spoken→transcribed) doubt here; we forward it to a LOCAL Ollama model on
# this machine and speak the Hindi reply back. This is the ONLY place that talks to
# the model, so safety, logging, and the system prompt all live server-side where
# client JS can't bypass them. The feature is purely ADDITIVE: if anything here fails
# the game falls back to the always-present scripted "Roshni" help. See SECURITY.md.
#
# Defence-in-depth (MVP floor): fail-CLOSED rate limit → require a consented, logged-in
# student → PII-redact before persisting → deterministic crisis short-circuit (never
# calls the model) → bounded Ollama call → output filter → log every turn for the
# facilitator review queue. A guard model + real-time crisis escalation come next.
# ---------------------------------------------------------------------------

# Fallback system prompt if Hikmat Settings has none (the editable copy lives in the
# Single so a facilitator can tune Roshni's voice without a code change).
_DEFAULT_AI_PROMPT = (
    'तुम "रोशनी" हो — चंपारण, बिहार की छोटी बच्चियों की एक प्यारी, मददगार टीचर-दीदी। '
    "एकदम आसान, रोज़मर्रा की हिंदी में बात करो (आम अंग्रेज़ी शब्द चल जाते हैं); कठिन या "
    "किताबी शब्द मत इस्तेमाल करो; छोटे-छोटे वाक्य। हमेशा हिम्मत देने वाले अंदाज़ में, "
    "जवाब छोटा रखो (2-4 वाक्य), फिर एक आसान सवाल पूछो। ग़लती पर डाँटो मत, प्यार से सही बताओ। "
    "सिर्फ़ पढ़ाई से जुड़ी बातें करो; कोई डरावनी, बड़ों वाली या ग़लत बात हो तो जवाब मत दो — "
    'कहो "ये बात किसी बड़े से पूछना, चलो कुछ मज़ेदार सीखते हैं!"। कभी फ़ोन नंबर, पता या '
    "निजी जानकारी मत माँगो। reasoning या सोच-विचार मत दिखाओ, सीधे हिंदी में जवाब दो।"
)
_DEFAULT_CRISIS_REPLY = (
    "यह बात किसी बड़े — अपनी टीचर या घर के किसी बड़े — से ज़रूर कहो, वे तुम्हारी मदद करेंगे। "
    "चलो, हम कुछ आसान और मज़ेदार सीखते हैं। 💛"
)
_PROMPT_VERSION = "mvp-1"

# Best-effort PII scrubbing BEFORE anything is persisted. Regex catches structured PII
# only — it will NOT catch a child naming a person/place in free text, so the stored
# transcript is "redacted on a best-effort basis", never guaranteed clean. (Design note.)
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_RE_AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_RE_PHONE = re.compile(r"\b\d{10}\b")
_RE_URL = re.compile(r"https?://\S+")

# Deterministic crisis lexicon — runs BEFORE the model so a disclosure never reaches an
# open-ended generator and always yields the safe escalation path. Crude on purpose
# (high recall, accepts false positives); the always-on "Tell the teacher" button and a
# facilitator review of every flagged row are the real safety net, not this list.
_CRISIS_TERMS = (
    "suicide", "kill myself", "kill me", "end my life", "self harm", "khudkushi",
    "atmahatya", "marna chahti", "marna chahta", "jaan dena", "jaan de",
    "rape", "molest", "abuse", "beating me", "hits me", "marta hai", "marti hai",
    "chhua", "galat kaam", "gande", "blood",
    "आत्महत्या", "मरना चाहती", "मरना चाहता", "जान दे", "जान देना",
    "छेड़", "पीटता", "पीटती", "गंदा", "गंदी", "मारता", "मारती",
)


def _rate_ok_strict(bucket, limit, seconds):
    """FAIL-CLOSED rate limit for the AI endpoint: if the cache is unavailable we DENY.
    The opposite of _rate_ok (which fails open for cheap, safe game writes) — an
    open-ended LLM must never run uncapped just because Redis hiccuped."""
    try:
        c = frappe.cache()
        key = "hikmat:rl:" + bucket
        n = _int(c.get_value(key), 0)
        if n >= limit:
            return False
        c.set_value(key, n + 1, expires_in_sec=seconds)
        return True
    except Exception:
        return False


def _redact(text):
    """Scrub structured PII. Returns (clean_text, was_redacted). Best-effort only."""
    if not text:
        return "", False
    out = _RE_EMAIL.sub("[email]", text)
    out = _RE_AADHAAR.sub("[number]", out)
    out = _RE_PHONE.sub("[number]", out)
    return out, (out != text)


def _is_crisis(text):
    t = (text or "").lower()
    return any(term in t for term in _CRISIS_TERMS)


def _filter_output(text):
    """Last gate on what gets spoken to / stored for a child: strip URLs, emails and bare
    digit strings (phone/Aadhaar) from the model's reply. Best-effort, same as _redact."""
    out = _RE_URL.sub("", text or "")
    out = _RE_EMAIL.sub("", out)
    out = _RE_AADHAAR.sub("", out)
    out = _RE_PHONE.sub("", out)
    return out.strip()


def _log_ai_turn(student, sinfo, ctx, conversation_id, client_turn_id, prompt, reply,
                 model, was_canned, flagged, flag_reason, redacted, latency_ms):
    """Upsert the parent AI Conversation (by conversation_id) and insert one Turn.
    Best-effort — a logging failure must never break the child's answer."""
    try:
        conv = None
        if conversation_id:
            conv = frappe.db.get_value("AI Conversation", {"conversation_id": conversation_id}, "name")
        if conv:
            if flagged:
                frappe.db.set_value("AI Conversation", conv, {
                    "flagged": 1, "flag_reason": (flag_reason or "")[:140],
                    "escalated": 1 if flag_reason == "crisis" else 0,
                }, update_modified=False)
        else:
            try:
                conv = frappe.get_doc({
                    "doctype": "AI Conversation",
                    "conversation_id": conversation_id or frappe.generate_hash(length=24),
                    "student": student, "student_name": sinfo.get("student_name"),
                    "cohort": sinfo.get("cohort"),
                    "track": ctx["track"], "lesson": ctx["lesson"], "activity": ctx["activity"],
                    "lang": ctx["lang"], "model": (model or "")[:140],
                    "flagged": flagged, "flag_reason": (flag_reason or "")[:140],
                    "escalated": 1 if flag_reason == "crisis" else 0,
                    "started_on": frappe.utils.now(),
                }).insert(ignore_permissions=True).name
            except frappe.DuplicateEntryError:                # raced with another turn of the same convo
                frappe.db.rollback()
                conv = (frappe.db.get_value("AI Conversation", {"conversation_id": conversation_id}, "name")
                        if conversation_id else None)

        if not conv:   # degraded path — the turn would be an orphan, invisible to the review queue
            frappe.log_error("hikmat: AI turn has no parent conversation (orphan); flagged=" + str(flagged),
                             "hikmat ai_ask")

        if client_turn_id and frappe.db.get_value("AI Conversation Turn", {"client_turn_id": client_turn_id}, "name"):
            return conv                                       # idempotent: this turn already logged
        try:
            frappe.get_doc({
                "doctype": "AI Conversation Turn", "conversation": conv,
                "student": student, "cohort": sinfo.get("cohort"),
                "track": ctx["track"], "lesson": ctx["lesson"], "activity": ctx["activity"],
                "prompt": (prompt or "")[:2000], "reply": (reply or "")[:2000],
                "lang": ctx["lang"], "model_version": (model or "")[:140],
                "prompt_version": _PROMPT_VERSION, "latency_ms": _int(latency_ms),
                "was_canned": 1 if was_canned else 0, "redaction_applied": 1 if redacted else 0,
                "flagged": 1 if flagged else 0, "client_turn_id": client_turn_id or None,
                "created_on": frappe.utils.now(),
            }).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
        frappe.db.commit()
        return conv
    except Exception:
        frappe.log_error("hikmat: ai turn logging failed", frappe.get_traceback())
        return None


@frappe.whitelist(allow_guest=True)
def ai_ask(student=None, token=None, track=None, lesson=None, activity=None,
           prompt=None, lang=None, conversation_id=None, client_turn_id=None):
    """Forward a child's doubt to the local Ollama tutor and return Roshni's Hindi reply.
    Requires a logged-in, consented student (guest free-text can't be consented or erased,
    so no anonymous AI). Everything is attacker-controllable → fail-closed, redact, cap."""
    if not _rate_ok_strict("ai:ip:" + _client_ip(), 120, 3600):   # per-IP hourly ceiling
        return {"ok": False, "error": "rate_limited"}
    if not student:
        return {"ok": False, "error": "login_required"}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _token_ok(student, token):
        return {"ok": False, "error": "auth"}
    if not _rate_ok_strict("ai:stu:" + str(student), 40, 3600):   # per-student hourly cap
        return {"ok": False, "error": "rate_limited"}

    s = frappe.get_single("Hikmat Settings")
    if not s.get("ai_enabled"):
        return {"ok": False, "error": "ai_off"}

    msg = (prompt or "").strip()[:2000]
    if not msg:
        return {"ok": False, "error": "empty"}

    ctx = {"track": (track or "")[:140], "lesson": (lesson or "")[:140],
           "activity": (activity or "")[:140], "lang": (lang or "")[:10]}
    model = (s.get("ai_model") or "gemma4:12b-mlx").strip()

    red_msg, redacted = _redact(msg)

    # Crisis short-circuit — never calls the model; serves a safe canned reply + flags
    # the conversation for facilitator review. (Real-time escalation to the named
    # Safeguarding Lead is the next step, pending that person being named.)
    if _is_crisis(red_msg):
        reply = (s.get("ai_crisis_reply") or _DEFAULT_CRISIS_REPLY)
        logged = _log_ai_turn(student, sinfo, ctx, conversation_id, client_turn_id, red_msg, reply,
                              model=model, was_canned=1, flagged=1, flag_reason="crisis",
                              redacted=redacted, latency_ms=0)
        if not logged:   # safeguarding path must never fail silently — leave a distinct trail
            frappe.log_error("hikmat: CRISIS disclosure flag may NOT have persisted — check this; student="
                             + str(student), "hikmat ai_ask CRISIS")
        return {"ok": True, "reply": reply, "flagged": True}

    sys_prompt = (s.get("ai_system_prompt") or _DEFAULT_AI_PROMPT)
    endpoint = (s.get("ai_endpoint") or "http://localhost:11434").rstrip("/")

    import time
    import requests                                           # lazy import — only when AI is used
    t0 = time.monotonic()
    try:
        r = requests.post(endpoint + "/api/chat", json={
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": red_msg}],
            "options": {"temperature": 0.6, "num_ctx": 4096, "num_predict": 160, "repeat_penalty": 1.1},
            "keep_alive": "30m",
        }, timeout=45)   # first call cold-loads the 6.8GB model (~slow); keep_alive holds it warm after
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception:                                         # Ollama down / timeout → scripted fallback
        return {"ok": False, "error": "ai_unavailable"}
    if not r.ok:
        return {"ok": False, "error": "ai_unavailable"}
    try:
        reply = ((r.json().get("message") or {}).get("content") or "").strip()
    except Exception:
        reply = ""
    reply = _filter_output(reply)[:1200]
    if not reply:
        return {"ok": False, "error": "ai_unavailable"}

    _log_ai_turn(student, sinfo, ctx, conversation_id, client_turn_id, red_msg, reply,
                 model=model, was_canned=0, flagged=0, flag_reason="", redacted=redacted,
                 latency_ms=latency_ms)
    return {"ok": True, "reply": reply}


# ---------------------------------------------------------------------------
# Roshni VOICE — local Whisper (STT) + Piper (TTS), proxied through Frappe so the browser
# uses ONE same-origin gateway (no CORS) and both model daemons stay bound to 127.0.0.1,
# unreachable from the school LAN. Audio is forwarded, NEVER persisted — only the transcript
# is logged later via ai_ask (text-only privacy posture). ANE is broken on M4+macOS26, so
# Whisper runs on Metal and shares the GPU with the tutor LLM → callers MUST single-flight
# STT→LLM→TTS (enforced client-side). Piper is CPU-only, so TTS can overlap the LLM.
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def ai_transcribe(student=None, token=None, lang="hi"):
    """Forward a short captured WAV to the local whisper-server; return the transcript only.
    Requires a logged-in, consented student (same gate as ai_ask). Stores no audio."""
    if not _rate_ok_strict("stt:ip:" + _client_ip(), 240, 3600):
        return {"ok": False, "error": "rate_limited"}
    if not student:
        return {"ok": False, "error": "login_required"}
    sinfo = frappe.db.get_value("Student", student, ["active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _token_ok(student, token):
        return {"ok": False, "error": "auth"}
    if not _rate_ok_strict("stt:stu:" + str(student), 80, 3600):
        return {"ok": False, "error": "rate_limited"}

    s = frappe.get_single("Hikmat Settings")
    if not s.get("voice_enabled"):
        return {"ok": False, "error": "voice_off"}
    f = frappe.request.files.get("audio") if getattr(frappe, "request", None) else None
    if not f:
        return {"ok": False, "error": "no_audio"}
    audio = f.read()
    if not audio or len(audio) > 4 * 1024 * 1024:   # ~4MB cap — a short push-to-talk clip
        return {"ok": False, "error": "bad_audio"}
    endpoint = (s.get("stt_endpoint") or "http://127.0.0.1:8080").rstrip("/")

    import requests
    try:
        r = requests.post(endpoint + "/inference",
                          files={"file": ("clip.wav", audio, "audio/wav")},
                          data={"language": (lang or "hi")[:5], "response_format": "json", "temperature": "0"},
                          timeout=20)
    except Exception:
        return {"ok": False, "error": "stt_unavailable"}
    if not r.ok:
        return {"ok": False, "error": "stt_unavailable"}
    try:
        text = (r.json().get("text") or "").strip()
    except Exception:
        text = (r.text or "").strip()
    return {"ok": True, "text": text[:2000]}


@frappe.whitelist(allow_guest=True)
def ai_tts(student=None, token=None, text=None):
    """Synthesize a short Hindi line via the local Piper server; return WAV bytes. The client
    caches by text so each phrase is synthesized once. Login required (v1 routes only Roshni's
    replies through neural TTS; general app narration stays on the browser voice). Piper's voice
    is fixed at server launch (see tts_voice / the setup script), so only text is sent."""
    if not _rate_ok_strict("tts:ip:" + _client_ip(), 600, 3600):
        return {"ok": False, "error": "rate_limited"}
    if not student or not _token_ok(student, token):
        return {"ok": False, "error": "auth"}
    s = frappe.get_single("Hikmat Settings")
    if not s.get("voice_enabled"):
        return {"ok": False, "error": "voice_off"}
    msg = (text or "").strip()[:600]
    if not msg:
        return {"ok": False, "error": "empty"}
    endpoint = (s.get("tts_endpoint") or "http://127.0.0.1:5000").rstrip("/")

    import requests
    try:                                              # Piper http server: POST raw UTF-8 text → WAV
        r = requests.post(endpoint, data=msg.encode("utf-8"),
                          headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=20)
    except Exception:
        return {"ok": False, "error": "tts_unavailable"}
    if not r.ok or not r.content:
        return {"ok": False, "error": "tts_unavailable"}
    frappe.response["type"] = "binary"
    frappe.response["filename"] = "roshni.wav"
    frappe.response["filecontent"] = r.content
    return


# ---------------------------------------------------------------------------
# Student login (facilitator-managed or self-signup; no email/password)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_csrf():
    """The game is a static page, so it has no CSRF token — but when the browser
    carries a logged-in session (a facilitator with Desk open, or an online student
    after /api/method/login), Frappe REJECTS token-less POSTs (CSRFTokenError → 400).
    This GET hands the page its own session's token; same-origin policy keeps other
    sites from reading it. Guests get "" (their POSTs aren't CSRF-checked)."""
    if frappe.session and frappe.session.user and frappe.session.user != "Guest":
        return {"token": frappe.sessions.get_csrf_token()}
    return {"token": ""}


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
    if not s.login_pin:                                 # PIN-less profile → un-loginnable; facilitator sets one in Desk
        return {"ok": False, "error": "no_pin"}
    if not _pin_ok(s.login_pin, pin):
        c.set_value(fkey, _int(c.get_value(fkey)) + 1, expires_in_sec=_LOCKOUT_SECONDS)
        return {"ok": False, "error": "wrong_pin"}
    c.delete_value(fkey)                                # reset the counter on success
    if not _looks_hashed(s.login_pin):                  # upgrade a legacy plaintext PIN to a hash on login
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
    if not (pin.isdigit() and 4 <= len(pin) <= 8):   # PIN now REQUIRED (4–8 digits) — no PIN-less profiles
        return {"ok": False, "error": "bad_pin"}
    a = _int(age, None)
    age_val = a if (a is not None and 3 <= a <= 25) else None
    band = band if (band and frappe.db.exists("Grade Band", band)) else None

    if not cohort:
        cohort = "Online"                                  # self-signups are the online cohort
        if not frappe.db.exists("Cohort", cohort):
            try:
                frappe.get_doc({"doctype": "Cohort", "cohort_name": cohort, "mode": "Online",
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
    # Filter by name in SQL (indexed via Student.student_name search_index; case-insensitive
    # under the default ci collation) instead of loading every active student into Python.
    cands = frappe.get_all("Student", filters={"active": 1, "student_name": key},
                           fields=["name", "student_name", "login_pin", "avatar", "band"])
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
# Online enrolment — a remote learner self-registers with a per-cohort INVITE CODE.
# Online students are real Frappe Website Users (username + PIN, synthetic no-email
# address, login-by-username) paired 1:1 with a Student record that holds their
# progress. Campus students never come through here (they're facilitator-created).
# ---------------------------------------------------------------------------
_ONLINE_EMAIL_DOMAIN = "students.hikmat.invalid"   # RFC-2606 non-routable → no mail ever leaves
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")


def _create_online_user(username, pin, full_name):
    """Create a login-only Website User: username + PIN (as the password), a synthetic
    non-routable email, no welcome mail, no Desk. Password policy is disabled site-wide
    for these PIN-based accounts (see patch v2_online_auth); staff rely on 2FA."""
    user = frappe.get_doc({
        "doctype": "User", "email": username + "@" + _ONLINE_EMAIL_DOMAIN,
        "first_name": full_name or username, "username": username,
        "user_type": "Website User", "send_welcome_email": 0, "enabled": 1,
        "new_password": pin,
    })
    user.flags.no_welcome_mail = True
    user.flags.ignore_password_policy = True
    user.insert(ignore_permissions=True)
    return user


@frappe.whitelist(allow_guest=True)
def signup_online(username=None, pin=None, invite_code=None, name=None, avatar=None, band=None):
    """Self-service ONLINE signup gated by a cohort invite code. Creates a Website User +
    linked Student (mode=Online). Rate-limited per IP. Generic errors (no enumeration)."""
    if not _rate_ok("signup:" + _client_ip(), 60, 3600):
        return {"ok": False, "error": "rate_limited"}
    username = (username or "").strip().lower()
    if not _USERNAME_RE.match(username):
        return {"ok": False, "error": "bad_username"}
    pin = (pin or "").strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        return {"ok": False, "error": "bad_pin"}
    cohort = frappe.db.get_value("Cohort", {"invite_code": (invite_code or "").strip()}, "name") \
        if (invite_code or "").strip() else None
    if not cohort:
        return {"ok": False, "error": "bad_invite"}
    if frappe.db.exists("User", {"username": username}) \
            or frappe.db.exists("User", username + "@" + _ONLINE_EMAIL_DOMAIN):
        return {"ok": False, "error": "username_taken"}
    name = "".join(ch for ch in (name or username).strip() if ch.isprintable())[:40] or username
    band = band if (band and frappe.db.exists("Grade Band", band)) else None

    user = _create_online_user(username, pin, name)
    stu = frappe.get_doc({
        "doctype": "Student", "student_name": name, "avatar": avatar or "🙂",
        "cohort": cohort, "login_pin": _hash_pin(pin), "active": 1, "gender": "Other",
        "band": band, "mode": "Online", "user": user.name,
    }).insert(ignore_permissions=True)
    token = _token_for(stu.name)
    frappe.db.commit()
    return {"ok": True, "id": stu.name, "name": name, "avatar": stu.avatar or "🙂",
            "token": token, "band": band or "", "username": username}


@frappe.whitelist(allow_guest=True)
def get_my_student():
    """After an ONLINE Frappe login (POST /api/method/login with the username + PIN), the game
    calls this over the same session to load the linked Student's profile + a bearer token —
    the client never needs to know the student id up front. Returns {ok:False} for a guest."""
    sid = _session_student()
    if not sid:
        return {"ok": False}
    s = frappe.db.get_value("Student", sid, ["student_name", "avatar", "band"], as_dict=True)
    return {"ok": True, "id": sid, "name": s.student_name, "avatar": s.avatar or "🙂",
            "band": s.band or "", "token": _token_for(sid)}   # request-level commit persists the token


@frappe.whitelist()   # NOT allow_guest → facilitator / System Manager only (returns secrets)
def get_campus_roster(campus=None):
    """Provision a campus laptop for offline login: the active campus roster WITH each
    girl's PIN hash + bearer token, cached on-device so name+PIN can be verified locally
    during an offline stretch and attempts synced on reconnect. Authorized (Desk) only —
    it returns credentials, so it must never be public."""
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if not campus:
        return []
    rows = frappe.get_all("Student", filters={"active": 1, "mode": "Campus", "campus": campus},
                          fields=["name", "student_name", "avatar", "login_pin", "band"],
                          order_by="student_name asc")
    out = [{"id": r.name, "name": r.student_name, "avatar": r.avatar or "🙂",
            "pinHash": r.login_pin or "", "token": _token_for(r.name), "band": r.band or ""} for r in rows]
    frappe.db.commit()   # _token_for may have minted tokens
    return out


@frappe.whitelist(allow_guest=True)
def get_campuses():
    """Active campuses (name + location) — for the device-setup screen's campus picker."""
    return frappe.get_all("Campus", filters={"active": 1}, fields=["name", "location"],
                          order_by="campus_name asc")


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
def get_progress(student=None, token=None):
    """Best stars per track/lesson/activity for one student — so progress follows the
    girl across shared laptops. Requires the student's login token (campus) or a matching
    online session (no reading another child's progress by guessing an id). Aggregated in
    SQL so the response is one row per (track,lesson,activity) regardless of replays."""
    if not student:
        student = _session_student()                         # online client authed by session
    if not student or not _authorized(student, token):
        return {"progress": {}}
    rows = frappe.db.sql(
        """select track, lesson, activity, max(stars) as stars
           from `tabLesson Attempt` where student=%s
           group by track, lesson, activity""",
        student, as_dict=True)
    prog = {}
    for r in rows:
        prog.setdefault(r.track, {}).setdefault(r.lesson, {})[r.activity] = r.stars or 0
    gates = {e.milestone: e.status for e in frappe.get_all(
        "Evaluation", filters={"student": student}, fields=["milestone", "status"])}
    # Module tests: pass/best per track, plus the union of every question id this
    # student has ever been served (ordered by first exposure) — so a girl on a NEW
    # device keeps her no-repeat guarantee once she's online. Bounded by bank sizes.
    tests = {}
    for r in frappe.db.sql(
            """select track, max(passed) as passed, max(pct) as best, count(*) as attempts
               from `tabTest Attempt` where student=%s group by track""", student, as_dict=True):
        tests[r.track] = {"passed": bool(r.passed), "bestPct": _int(r.best), "attempts": _int(r.attempts)}
    test_seen = {}
    if tests:
        for a in frappe.get_all("Test Attempt", filters={"student": student},
                                fields=["track", "paper"], order_by="attempted_on asc, creation asc"):
            try:
                ids = json.loads(a.paper or "[]")
            except Exception:
                ids = []
            dst = test_seen.setdefault(a.track, [])
            for qid in ids:
                if qid not in dst:
                    dst.append(qid)
    return {"progress": prog, "gates": gates, "gems": _total_gems(student),
            "tests": tests, "testSeen": test_seen}


@frappe.whitelist()   # NOT allow_guest → requires a logged-in Desk user (facilitator / System Manager)
def delete_student(student):
    """Erase a child's record and ALL their attempts (right-to-erasure for minors' data).
    Facilitator-only. Use from Desk or a trusted admin tool."""
    if not frappe.db.exists("Student", student):
        return {"ok": False, "error": "not_found"}
    name = frappe.db.get_value("Student", student, "student_name")
    # Erase every record that carries this child's data, children before parents.
    # (Lesson Doubt was previously missed — fixed here alongside the new AI tables.)
    for turn in frappe.get_all("AI Conversation Turn", filters={"student": student}, pluck="name"):
        frappe.delete_doc("AI Conversation Turn", turn, force=1, ignore_permissions=True)
    for conv in frappe.get_all("AI Conversation", filters={"student": student}, pluck="name"):
        frappe.delete_doc("AI Conversation", conv, force=1, ignore_permissions=True)
    for d in frappe.get_all("Lesson Doubt", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Lesson Doubt", d, force=1, ignore_permissions=True)
    for att in frappe.get_all("Lesson Attempt", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Lesson Attempt", att, force=1, ignore_permissions=True)
    for ta in frappe.get_all("Test Attempt", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Test Attempt", ta, force=1, ignore_permissions=True)
    for ev in frappe.get_all("Learning Event", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Learning Event", ev, force=1, ignore_permissions=True)
    for ap in frappe.get_all("Attendance Ping", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Attendance Ping", ap, force=1, ignore_permissions=True)
    for ad in frappe.get_all("Attendance Day", filters={"student": student}, pluck="name"):
        frappe.delete_doc("Attendance Day", ad, force=1, ignore_permissions=True)
    frappe.delete_doc("Student", student, force=1, ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "deleted": name}


@frappe.whitelist()   # NOT allow_guest → facilitator / System Manager only
def revoke_student_token(student):
    """Force a student to re-login everywhere: rotate their auth_token to a fresh value so any
    cached token (e.g. on a lost/handed-down laptop) stops working immediately. Use from Desk."""
    if not frappe.db.exists("Student", student):
        return {"ok": False, "error": "not_found"}
    frappe.db.set_value("Student", student,
                        {"auth_token": frappe.generate_hash(length=40),
                         "token_issued_on": frappe.utils.now()},
                        update_modified=False)
    frappe.db.commit()
    return {"ok": True}
