"""
Hikmat — DocType definitions + content seed (run via `bench execute`).

The DocTypes mirror the game's COURSES shape exactly so the API can return
the same JSON the offline game already consumes (see hikmat/api.py).

  Track ──< Lesson ──< Lesson Word (child)
                   └─ Dialogue ──< Dialogue Reply (child)
  Cohort ──< Student ──< Lesson Attempt
  Hikmat Settings (single)

Usage:
  bench --site hikmat.local execute hikmat.setup_data.create_doctypes
  bench --site hikmat.local migrate
  bench --site hikmat.local execute hikmat.setup_data.seed_content
"""
import json
import frappe

MODULE = "Hikmat"


def f(fieldname, fieldtype, label=None, **kw):
    d = {"fieldname": fieldname, "fieldtype": fieldtype,
         "label": label or fieldname.replace("_", " ").title()}
    d.update(kw)
    return d


def _mk(name, fields, autoname=None, istable=0, issingle=0, title_field=None, search_fields=None):
    if frappe.db.exists("DocType", name):
        print("skip (exists):", name)
        return
    perms = [] if istable else [{
        "role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1,
        "report": 1, "export": 1, "email": 1, "print": 1, "share": 1,
    }]
    frappe.get_doc({
        "doctype": "DocType",
        "name": name,
        "module": MODULE,
        "custom": 0,
        "istable": istable,
        "issingle": issingle,
        "editable_grid": 1,
        "engine": "InnoDB",
        "autoname": autoname,
        "title_field": title_field,
        "search_fields": search_fields,
        "fields": fields,
        "permissions": perms,
    }).insert()
    print("created:", name)


def create_doctypes():
    # --- child tables first (referenced by Table fields) ---
    _mk("Lesson Word", [
        f("en", "Data", "English", reqd=1, in_list_view=1),
        f("hi", "Data", "Hindi meaning", in_list_view=1),
        f("pron", "Data", "Pronunciation (Devanagari)", in_list_view=1),
        f("emoji", "Data", "Emoji"),
        f("word_type", "Data", "Word type (thing/mass/person/verb/adj/time/phrase)"),
        f("uncountable", "Check", "Uncountable (mass noun → 'some')"),
        f("plural", "Data", "Plural form (if countable)"),
        f("use_en", "Data", "Example sentence (overrides auto Build-a-Sentence)"),
        f("use_hi", "Data", "Example sentence (Hindi hint)"),
    ], istable=1)

    _mk("Dialogue Reply", [
        f("text", "Data", "Reply text", reqd=1, in_list_view=1),
        f("text_hi", "Data", "Reply text (Hindi gloss)"),
        f("is_correct", "Check", "Correct?", in_list_view=1),
    ], istable=1)

    _mk("Lesson Code", [
        f("prompt", "Data", "Prompt (English instruction)", reqd=1, in_list_view=1),
        f("prompt_hi", "Data", "Prompt (Hindi)"),
        f("teach", "Small Text", "Theory note (explains the concept)"),
        f("teach_hi", "Small Text", "Theory note (Hindi)"),
        f("code", "Small Text", "Code (use ___ for the blank, newlines for lines)", reqd=1, in_list_view=1),
        f("choices", "Small Text", "Choices (one per line)", reqd=1),
        f("answer", "Data", "Answer (correct choice)", reqd=1, in_list_view=1),
    ], istable=1)

    _mk("Lesson Fix", [
        f("sentence", "Data", "Sentence (with one wrong word)", reqd=1, in_list_view=1),
        f("wrong_word", "Data", "Wrong word (one token of the sentence)", reqd=1, in_list_view=1),
        f("correction", "Data", "Correction (replaces the wrong word)", reqd=1, in_list_view=1),
        f("teach", "Small Text", "Why (explains the fix)"),
        f("teach_hi", "Small Text", "Why (Hindi)"),
    ], istable=1)

    _mk("Lesson Email", [
        f("scenario", "Data", "Scenario (English instruction)", reqd=1, in_list_view=1),
        f("scenario_hi", "Data", "Scenario (Hindi)"),
        f("spec_json", "Long Text", "Email spec (JSON: to, from, slots[])", reqd=1),
    ], istable=1)

    # generic multiple-choice question — works for any subject (Math, Science, GK…)
    _mk("Lesson Quiz", [
        f("question", "Data", "Question (English)", reqd=1, in_list_view=1),
        f("question_hi", "Data", "Question (Hindi)"),
        f("emoji", "Data", "Picture / emoji (optional, shown above the question)"),
        f("choices", "Small Text", "Choices (one per line)", reqd=1),
        f("answer", "Data", "Answer (correct choice)", reqd=1, in_list_view=1),
        f("teach", "Small Text", "Why (explains the answer)"),
        f("teach_hi", "Small Text", "Why (Hindi)"),
    ], istable=1)

    # reading comprehension — a short passage the child reads (or hears) before answering
    _mk("Lesson Read", [
        f("title", "Data", "Title (English)", in_list_view=1),
        f("title_hi", "Data", "Title (Hindi)"),
        f("emoji", "Data", "Picture / emoji (optional, shown by the title)"),
        f("passage", "Small Text", "Passage (English) — a short simple story", reqd=1, in_list_view=1),
        f("passage_hi", "Small Text", "Passage (Hindi gloss)"),
        f("question", "Data", "Question (English)", reqd=1, in_list_view=1),
        f("question_hi", "Data", "Question (Hindi)"),
        f("choices", "Small Text", "Choices (one per line)", reqd=1),
        f("answer", "Data", "Answer (correct choice)", reqd=1, in_list_view=1),
        f("teach", "Small Text", "Why (explains the answer)"),
        f("teach_hi", "Small Text", "Why (Hindi)"),
    ], istable=1)

    # --- structure: grade bands + subjects (Class 1–10 grouping) ---
    _mk("Grade Band", [
        f("band_key", "Data", "Key (slug, e.g. 1-4)", reqd=1, unique=1, in_list_view=1),
        f("title", "Data", "Title (e.g. Class 1–4)", reqd=1, in_list_view=1),
        f("title_hi", "Data", "Title (Hindi)"),
        f("subtitle", "Data", "Subtitle / blurb"),
        f("subtitle_hi", "Data", "Subtitle (Hindi)"),
        f("icon", "Data", "Icon (emoji)"),
        f("color", "Data", "Color (hex)"),
        f("sort_order", "Int", "Sort order"),
        f("published", "Check", "Published", default="1"),
    ], autoname="field:band_key", title_field="title")

    _mk("Subject", [
        f("subject_key", "Data", "Key (slug, e.g. english)", reqd=1, unique=1, in_list_view=1),
        f("title", "Data", "Title (e.g. English)", reqd=1, in_list_view=1),
        f("title_hi", "Data", "Title (Hindi)"),
        f("icon", "Data", "Icon (emoji)"),
        f("color", "Data", "Color (hex)"),
        f("sort_order", "Int", "Sort order"),
    ], autoname="field:subject_key", title_field="title")

    # --- main doctypes ---
    _mk("Track", [
        f("track_key", "Data", "Key (slug)", reqd=1, unique=1, in_list_view=1),
        f("title", "Data", "Title", reqd=1, in_list_view=1),
        f("title_hi", "Data", "Title (Hindi)"),
        f("icon", "Data", "Icon (emoji)"),
        f("color", "Data", "Color (hex)"),
        f("blurb", "Data", "Blurb"),
        f("blurb_hi", "Data", "Blurb (Hindi)"),
        f("sort_order", "Int", "Sort order"),
        f("published", "Check", "Published", default="1"),
    ], autoname="field:track_key", title_field="title")

    _mk("Lesson", [
        f("track", "Link", "Track", options="Track", reqd=1, in_list_view=1),
        f("lesson_key", "Data", "Key (slug)", reqd=1, in_list_view=1),
        f("title", "Data", "Title", reqd=1, in_list_view=1),
        f("title_hi", "Data", "Title (Hindi)"),
        f("sort_order", "Int", "Sort order"),
        f("published", "Check", "Published", default="1"),
        f("sec_words", "Section Break", "Words"),
        f("words", "Table", "Words", options="Lesson Word"),
        f("sec_code", "Section Break", "Code challenges"),
        f("code", "Table", "Code challenges", options="Lesson Code"),
        f("sec_fix", "Section Break", "Find-the-bug sentences"),
        f("fix", "Table", "Find-the-bug sentences", options="Lesson Fix"),
        f("sec_email", "Section Break", "Email tasks"),
        f("email", "Table", "Email tasks", options="Lesson Email"),
    ], autoname="format:{track}-{lesson_key}", title_field="title")

    _mk("Dialogue", [
        f("lesson", "Link", "Lesson", options="Lesson", reqd=1, in_list_view=1),
        f("who", "Data", "Speaker (emoji)", default="🧑‍🦱"),
        f("line", "Data", "Line (English)", reqd=1, in_list_view=1),
        f("line_hi", "Data", "Line (Hindi)"),
        f("followup", "Data", "Follow-up reply"),
        f("sort_order", "Int", "Sort order"),
        f("sec_replies", "Section Break", "Replies"),
        f("replies", "Table", "Replies", options="Dialogue Reply"),
    ], autoname="hash", title_field="line")

    # Controlled list of intake dates — Cohort.start_date is a dropdown over these,
    # so batches can only start on dates the admin has explicitly planned.
    _mk("Cohort Start Date", [
        f("start_date", "Date", "Start date", reqd=1, in_list_view=1),
    ], autoname="format:{start_date}", title_field="start_date")

    _mk("Cohort", [
        f("cohort_name", "Data", "Cohort name", reqd=1, unique=1, in_list_view=1),
        f("mode", "Select", "Mode", options="\nOffline\nOnline", default="Offline",
          reqd=1, in_list_view=1),
        f("start_date", "Link", "Start date", options="Cohort Start Date",
          mandatory_depends_on='eval:doc.mode=="Offline"', in_list_view=1),
        f("center", "Data", "Center / location"),
        f("facilitator", "Data", "Facilitator"),
    ], autoname="field:cohort_name", title_field="cohort_name")

    _mk("Student", [
        f("student_name", "Data", "Name", reqd=1, in_list_view=1),
        f("age", "Int", "Age", in_list_view=1),
        f("gender", "Select", "Gender", options="\nFemale\nMale\nOther"),
        f("cohort", "Link", "Cohort", options="Cohort", in_list_view=1),
        f("login_pin", "Data", "Login PIN"),
        f("avatar", "Data", "Avatar (emoji)"),
        f("active", "Check", "Active", default="1"),
    ], autoname="hash", title_field="student_name", search_fields="student_name")

    _mk("Lesson Attempt", [
        f("student", "Link", "Student", options="Student", reqd=1, in_list_view=1),
        f("track", "Data", "Track key", in_list_view=1),
        f("lesson", "Data", "Lesson key", in_list_view=1),
        f("activity", "Data", "Activity", in_list_view=1),
        f("stars", "Int", "Stars", in_list_view=1),
        f("score", "Int", "Score"),
        f("total", "Int", "Total"),
        f("coins", "Int", "Coins"),
        f("attempted_on", "Datetime", "Attempted on"),
    ], autoname="hash", title_field="student")

    # "Roshni, mujhe doubt hai" — every time a learner taps for help, one row lands here.
    # The facilitator confusion heatmap (Layer 2) reads from this DocType.
    _mk("Lesson Doubt", [
        f("student", "Link", "Student", options="Student", in_list_view=1),
        f("student_name", "Data", "Student name", in_list_view=1),
        f("cohort", "Link", "Cohort", options="Cohort", in_list_view=1),
        f("track", "Data", "Track key", in_list_view=1),
        f("lesson", "Data", "Lesson key", in_list_view=1),
        f("activity", "Data", "Activity", in_list_view=1),
        f("question", "Small Text", "What she was stuck on"),
        f("lang", "Data", "UI language"),
        f("client_id", "Data", "Client id (idempotency)", unique=1, no_copy=1),
        f("raised_on", "Datetime", "Raised on", in_list_view=1),
        f("resolved", "Check", "Resolved by facilitator"),
    ], autoname="hash", title_field="student_name")

    # Learning events — the fine-grained analytics stream. One row per notable moment;
    # kind="wrong_answer" today (question + what she picked vs the right answer), so a
    # facilitator can see WHICH question breaks down, not just the final score.
    _mk("Learning Event", [
        f("student", "Link", "Student", options="Student", in_list_view=1),
        f("student_name", "Data", "Student name", in_list_view=1),
        f("cohort", "Link", "Cohort", options="Cohort"),
        f("kind", "Data", "Event kind", in_list_view=1),
        f("track", "Data", "Track key", in_list_view=1),
        f("lesson", "Data", "Lesson key", in_list_view=1),
        f("activity", "Data", "Activity", in_list_view=1),
        f("question", "Data", "Question / prompt", in_list_view=1),
        f("chosen", "Data", "What she picked"),
        f("answer", "Data", "Correct answer"),
        f("lang", "Data", "UI language"),
        f("client_id", "Data", "Client id (idempotency)", unique=1, no_copy=1),
        f("occurred_on", "Datetime", "Occurred on", in_list_view=1),
    ], autoname="hash", title_field="question")

    # Milestone "belt" gates — admin-configurable star thresholds. When a girl's total
    # stars cross a threshold, new content locks until a facilitator evaluates her
    # in person and marks the Evaluation Passed (online-first clearing; the client only
    # DETECTS the gate locally — clearing is server-authoritative).
    _mk("Hikmat Milestone", [
        f("milestone_key", "Data", "Key", reqd=1, unique=1, in_list_view=1),
        f("title", "Data", "Title", reqd=1, in_list_view=1),
        f("title_hi", "Data", "Title (Hindi)"),
        f("icon", "Data", "Icon (emoji)"),
        f("threshold_gems", "Int", "Threshold (total gems 💎)", reqd=1, in_list_view=1),
        f("sort_order", "Int", "Sort order"),
        f("active", "Check", "Active", default="1", in_list_view=1),
    ], autoname="field:milestone_key", title_field="title")

    # One row per (student, milestone) — created Pending by submit_attempt when the
    # threshold is crossed; the facilitator fills in the rubric outcome in Desk.
    # autoname format makes student+milestone unique at the DB level.
    _mk("Evaluation", [
        f("student", "Link", "Student", options="Student", reqd=1, in_list_view=1),
        f("student_name", "Data", "Student name", in_list_view=1),
        f("cohort", "Link", "Cohort", options="Cohort"),
        f("campus", "Link", "Campus", options="Campus"),
        f("milestone", "Link", "Milestone", options="Hikmat Milestone", reqd=1, in_list_view=1),
        f("threshold_gems", "Int", "Threshold when reached"),
        f("gems_at_reach", "Int", "Gems when reached"),
        f("status", "Select", "Status", options="Pending\nPassed\nNeeds Practice",
          default="Pending", reqd=1, in_list_view=1),
        f("score", "Int", "Rubric score"),
        f("rubric_notes", "Small Text", "Rubric notes (speak / read / write)"),
        f("evaluated_by", "Link", "Evaluated by", options="User"),
        f("evaluated_on", "Datetime", "Evaluated on"),
        f("reached_on", "Datetime", "Reached on", in_list_view=1),
    ], autoname="format:EV-{student}-{milestone}", title_field="student_name")

    # Roshni AI — one row per tutoring SESSION (parent). Turns link back to it so the
    # facilitator review queue and confusion mining can GROUP BY turn.
    _mk("AI Conversation", [
        f("student", "Link", "Student", options="Student", in_list_view=1),
        f("student_name", "Data", "Student name", in_list_view=1),
        f("cohort", "Link", "Cohort", options="Cohort", in_list_view=1),
        f("track", "Data", "Track key"),
        f("lesson", "Data", "Lesson key"),
        f("activity", "Data", "Activity"),
        f("conversation_id", "Data", "Conversation id (idempotency)", unique=1, no_copy=1),
        f("lang", "Data", "UI language"),
        f("model", "Data", "Model"),
        f("flagged", "Check", "Flagged", in_list_view=1),
        f("flag_reason", "Data", "Flag reason"),
        f("escalated", "Check", "Escalated"),
        f("acknowledged_by", "Data", "Acknowledged by"),
        f("reviewed", "Check", "Reviewed by facilitator"),
        f("helpful", "Select", "Helpful?", options="\nyes\nno"),
        f("started_on", "Datetime", "Started on", in_list_view=1),
    ], autoname="hash", title_field="student_name")

    # ...one row per TURN: a child's redacted doubt + Roshni's reply + telemetry.
    _mk("AI Conversation Turn", [
        f("conversation", "Link", "Conversation", options="AI Conversation", in_list_view=1),
        f("student", "Link", "Student", options="Student"),
        f("cohort", "Link", "Cohort", options="Cohort"),
        f("track", "Data", "Track key"),
        f("lesson", "Data", "Lesson key"),
        f("activity", "Data", "Activity"),
        f("prompt", "Small Text", "Doubt (redacted)"),
        f("reply", "Small Text", "Roshni's reply"),
        f("lang", "Data", "UI language"),
        f("model_version", "Data", "Model version"),
        f("prompt_version", "Data", "Prompt version"),
        f("latency_ms", "Int", "Latency (ms)"),
        f("was_canned", "Check", "Canned (not generated)"),
        f("redaction_applied", "Check", "Redaction applied"),
        f("asr_confidence", "Float", "ASR confidence (voice)"),
        f("flagged", "Check", "Flagged", in_list_view=1),
        f("client_turn_id", "Data", "Client turn id (idempotency)", unique=1, no_copy=1),
        f("created_on", "Datetime", "Created on", in_list_view=1),
    ], autoname="hash", title_field="conversation")

    _mk("Hikmat Settings", [
        f("app_name", "Data", "App name", default="Hikmat"),
        f("logo", "Attach Image", "Logo (replaces the diya 🪔 in the game header)"),
        f("tagline_en", "Data", "Tagline (English)", default="Learn English by playing"),
        f("tagline_hi", "Data", "Tagline (Hindi)"),
        f("default_language", "Select", "Default language", options="en\nhi", default="en"),
        f("help_default_on", "Check", "Hindi help on by default"),
        f("default_theme", "Select", "Default theme", options="light\ndark", default="light"),
        f("default_sound", "Check", "Sound on by default", default="1"),
        f("sec_ai", "Section Break", "Roshni AI (local Ollama tutor)"),
        f("ai_enabled", "Check", "Enable Roshni AI"),
        f("ai_model", "Data", "Ollama model", default="gemma4:12b-mlx"),
        f("ai_endpoint", "Data", "Ollama endpoint", default="http://localhost:11434"),
        f("ai_system_prompt", "Long Text", "Roshni system prompt (Hindi) — blank uses the built-in default"),
        f("ai_crisis_reply", "Small Text", "Crisis safe reply (Hindi) — blank uses the built-in default"),
        f("sec_voice", "Section Break", "Roshni voice (local Whisper STT + Piper TTS)"),
        f("voice_enabled", "Check", "Enable voice (mic in + neural Hindi voice out)"),
        f("stt_endpoint", "Data", "Whisper STT endpoint (local)", default="http://127.0.0.1:8080"),
        f("tts_endpoint", "Data", "Piper TTS endpoint (local)", default="http://127.0.0.1:5000"),
        f("tts_voice", "Data", "Piper Hindi voice", default="hi_IN-priyamvada-medium"),
    ], issingle=1)

    frappe.db.commit()
    print("=== create_doctypes done ===")


def add_auth_field():
    """Add the per-student auth_token + the student's grade band to Student. Safe to re-run."""
    st = frappe.get_doc("DocType", "Student")
    have = [x.fieldname for x in st.fields]
    changed = False
    if "auth_token" not in have:
        st.append("fields", {"fieldname": "auth_token", "fieldtype": "Data", "label": "Auth token",
                             "hidden": 1, "no_copy": 1, "read_only": 1, "print_hide": 1})
        changed = True
    if "token_issued_on" not in have:
        st.append("fields", {"fieldname": "token_issued_on", "fieldtype": "Datetime",
                             "label": "Token issued on",
                             "hidden": 1, "no_copy": 1, "read_only": 1, "print_hide": 1})
        changed = True
    if "band" not in have:
        st.append("fields", {"fieldname": "band", "fieldtype": "Link", "label": "Grade band",
                             "options": "Grade Band", "in_list_view": 1})
        changed = True
    if changed:
        st.save()
        frappe.db.commit()
    print("=== Student.auth_token + band ensured ===")


def update_cohort_fields():
    """Cohort = an Online batch (invite-code signup, start date optional) or an Offline
    campus batch (start date REQUIRED). Start date becomes a controlled dropdown over
    the Cohort Start Date doctype. Safe to re-run."""
    ct = frappe.get_doc("DocType", "Cohort")
    have = {x.fieldname: x for x in ct.fields}
    changed = False
    if "mode" not in have:
        ct.append("fields", {"fieldname": "mode", "fieldtype": "Select", "label": "Mode",
                             "options": "\nOffline\nOnline", "default": "Offline",
                             "reqd": 1, "in_list_view": 1})
        changed = True
    sd = have.get("start_date")
    if sd is not None and (sd.fieldtype != "Link" or not sd.mandatory_depends_on):
        sd.fieldtype = "Link"
        sd.options = "Cohort Start Date"
        sd.mandatory_depends_on = 'eval:doc.mode=="Offline"'
        sd.in_list_view = 1
        changed = True
    if changed:
        ct.save()
        frappe.db.commit()
    print("=== Cohort.mode + start_date dropdown ensured ===")


def add_structure_fields():
    """Add band+subject Link fields to Track and a quiz Table to Lesson.
    Mirrors add_attempt_fields() — safe to re-run (skips fields that exist)."""
    tr = frappe.get_doc("DocType", "Track")
    have = [x.fieldname for x in tr.fields]
    if "band" not in have:
        tr.append("fields", {"fieldname": "band", "fieldtype": "Link", "label": "Grade band",
                             "options": "Grade Band", "in_list_view": 1,
                             "insert_after": "blurb_hi"})
    if "subject" not in have:
        tr.append("fields", {"fieldname": "subject", "fieldtype": "Link", "label": "Subject",
                             "options": "Subject", "in_list_view": 1,
                             "insert_after": "band"})
    tr.save()

    ls = frappe.get_doc("DocType", "Lesson")
    have = [x.fieldname for x in ls.fields]
    if "quiz" not in have:
        ls.append("fields", {"fieldname": "sec_quiz", "fieldtype": "Section Break", "label": "Quiz questions"})
        ls.append("fields", {"fieldname": "quiz", "fieldtype": "Table", "label": "Quiz questions",
                             "options": "Lesson Quiz"})
    ls.save()
    frappe.db.commit()
    print("=== Track: band+subject added; Lesson: quiz table added ===")


def add_ai_fields():
    """Add the newer config fields (game defaults + Roshni-AI) to the EXISTING Hikmat Settings
    single (create_doctypes skips doctypes that already exist, so a live site needs this).
    Safe to re-run."""
    hs = frappe.get_doc("DocType", "Hikmat Settings")
    have = [x.fieldname for x in hs.fields]
    additions = [
        ("default_theme", {"fieldtype": "Select", "label": "Default theme", "options": "light\ndark", "default": "light"}),
        ("default_sound", {"fieldtype": "Check", "label": "Sound on by default", "default": "1"}),
        ("sec_ai", {"fieldtype": "Section Break", "label": "Roshni AI (local Ollama tutor)"}),
        ("ai_enabled", {"fieldtype": "Check", "label": "Enable Roshni AI"}),
        ("ai_model", {"fieldtype": "Data", "label": "Ollama model", "default": "gemma4:12b-mlx"}),
        ("ai_endpoint", {"fieldtype": "Data", "label": "Ollama endpoint", "default": "http://localhost:11434"}),
        ("ai_system_prompt", {"fieldtype": "Long Text", "label": "Roshni system prompt (Hindi) — blank uses the built-in default"}),
        ("ai_crisis_reply", {"fieldtype": "Small Text", "label": "Crisis safe reply (Hindi) — blank uses the built-in default"}),
        ("sec_voice", {"fieldtype": "Section Break", "label": "Roshni voice (local Whisper STT + Piper TTS)"}),
        ("voice_enabled", {"fieldtype": "Check", "label": "Enable voice (mic in + neural Hindi voice out)"}),
        ("stt_endpoint", {"fieldtype": "Data", "label": "Whisper STT endpoint (local)", "default": "http://127.0.0.1:8080"}),
        ("tts_endpoint", {"fieldtype": "Data", "label": "Piper TTS endpoint (local)", "default": "http://127.0.0.1:5000"}),
        ("tts_voice", {"fieldtype": "Data", "label": "Piper Hindi voice", "default": "hi_IN-priyamvada-medium"}),
    ]
    changed = False
    for fn, spec in additions:
        if fn not in have:
            hs.append("fields", {"fieldname": fn, **spec})
            changed = True
    if changed:
        hs.save()
        frappe.db.commit()
    print("=== Hikmat Settings: AI fields ensured ===")


# the three grade bands and the subject palette (icons/colours used by the game)
GRADE_BANDS = [
    {"key": "1-4",  "title": "Class 1–4",  "titleHi": "कक्षा 1–4",
     "subtitle": "Foundation — letters, numbers, the world around me",
     "subtitleHi": "नींव — अक्षर, अंक और मेरे आस-पास की दुनिया",
     "icon": "🌱", "color": "#22b8a6"},
    {"key": "5-8",  "title": "Class 5–8",  "titleHi": "कक्षा 5–8",
     "subtitle": "Middle — grammar, operations, science",
     "subtitleHi": "मध्य — व्याकरण, गणित और विज्ञान",
     "icon": "🌟", "color": "#f59e0b"},
    {"key": "9-10", "title": "Class 9–10", "titleHi": "कक्षा 9–10",
     "subtitle": "Secondary — board-level skills & computers",
     "subtitleHi": "माध्यमिक — बोर्ड स्तर के कौशल और कंप्यूटर",
     "icon": "🎓", "color": "#6c5ce7"},
]
SUBJECTS = [
    {"key": "english",  "title": "English",        "titleHi": "अंग्रेज़ी",   "icon": "🔤", "color": "#2ec27e"},
    {"key": "math",     "title": "Mathematics",    "titleHi": "गणित",        "icon": "➗", "color": "#3b82f6"},
    {"key": "science",  "title": "Science",        "titleHi": "विज्ञान",     "icon": "🔬", "color": "#06b6d4"},
    {"key": "evs",      "title": "EVS",            "titleHi": "पर्यावरण",    "icon": "🌍", "color": "#16a34a"},
    {"key": "hindi",    "title": "Hindi",          "titleHi": "हिंदी",       "icon": "📖", "color": "#e11d48"},
    {"key": "sst",      "title": "Social Studies", "titleHi": "सामाजिक अध्ययन", "icon": "🗺️", "color": "#d97706"},
    {"key": "computer", "title": "Computer",       "titleHi": "कंप्यूटर",    "icon": "💻", "color": "#8b5cf6"},
]


def seed_structure():
    """Create the grade bands + subjects (idempotent — updates if they exist)."""
    for i, b in enumerate(GRADE_BANDS):
        doc = frappe.get_doc("Grade Band", b["key"]) if frappe.db.exists("Grade Band", b["key"]) \
            else frappe.new_doc("Grade Band")
        doc.update({"band_key": b["key"], "title": b["title"], "title_hi": b["titleHi"],
                    "subtitle": b["subtitle"], "subtitle_hi": b["subtitleHi"],
                    "icon": b["icon"], "color": b["color"], "sort_order": i, "published": 1})
        doc.save(ignore_permissions=1)
    for i, s in enumerate(SUBJECTS):
        doc = frappe.get_doc("Subject", s["key"]) if frappe.db.exists("Subject", s["key"]) \
            else frappe.new_doc("Subject")
        doc.update({"subject_key": s["key"], "title": s["title"], "title_hi": s["titleHi"],
                    "icon": s["icon"], "color": s["color"], "sort_order": i})
        doc.save(ignore_permissions=1)
    frappe.db.commit()
    print("=== seeded", len(GRADE_BANDS), "bands +", len(SUBJECTS), "subjects ===")


# Milestone belts — CONFIGURABLE thresholds, never hardcoded in the client. Measured in
# GEMS 💎 (the game's earned currency: score*5 + stars*10 per attempt) — unlike stars,
# gems keep accumulating on replays, so practice counts toward the next belt.
MILESTONES = [
    {"key": "belt_1", "title": "Level 1 Belt", "titleHi": "स्तर 1 बेल्ट", "icon": "🟡", "threshold": 1000},
    {"key": "belt_2", "title": "Level 2 Belt", "titleHi": "स्तर 2 बेल्ट", "icon": "🟢", "threshold": 2500},
    {"key": "belt_3", "title": "Level 3 Belt", "titleHi": "स्तर 3 बेल्ट", "icon": "🔵", "threshold": 5000},
    {"key": "belt_4", "title": "Level 4 Belt", "titleHi": "स्तर 4 बेल्ट", "icon": "⚫", "threshold": 10000},
]


def seed_milestones():
    """Create/refresh the belt milestones (idempotent — updates thresholds if they exist)."""
    for i, m in enumerate(MILESTONES):
        doc = frappe.get_doc("Hikmat Milestone", m["key"]) if frappe.db.exists("Hikmat Milestone", m["key"]) \
            else frappe.new_doc("Hikmat Milestone")
        doc.update({"milestone_key": m["key"], "title": m["title"], "title_hi": m["titleHi"],
                    "icon": m["icon"], "threshold_gems": m["threshold"],
                    "sort_order": i, "active": 1})
        doc.save(ignore_permissions=1)
    frappe.db.commit()
    print("=== seeded", len(MILESTONES), "milestones ===")


def setup_evaluation_report():
    """Facilitator 'Pending Evaluations' list: who reached a belt and is waiting for an
    in-person rubric evaluation — oldest wait first, so nobody is left locked."""
    name = "Pending Evaluations"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        e.name           AS "Evaluation:Link/Evaluation:160",
        e.student_name   AS "Student::140",
        e.cohort         AS "Cohort::120",
        e.campus         AS "Campus::140",
        e.milestone      AS "Milestone::110",
        e.threshold_gems AS "Threshold:Int:100",
        e.gems_at_reach  AS "Gems:Int:90",
        e.reached_on     AS "Reached:Datetime:160"
    FROM `tabEvaluation` e
    WHERE e.status = 'Pending'
    ORDER BY e.reached_on ASC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Evaluation",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Pending Evaluations' ready ===")


def seed_operational_defaults():
    """Everything a FRESH site needs beyond content. Patches are marked complete —
    NOT executed — when the app installs on a new site, so the records and System
    Settings that dev picked up via patches v1–v4 must also be seeded here. Idempotent.
    """
    # the physical campus for the offline path
    if frappe.db.exists("DocType", "Campus") and not frappe.db.exists("Campus", "Noor Girls High School"):
        frappe.get_doc({"doctype": "Campus", "campus_name": "Noor Girls High School",
                        "location": "Meghwal Mathia", "active": 1}).insert(ignore_permissions=True)

    # the two-cohort model: Online (self-signup + invite) and the campus batch
    if not frappe.db.exists("Cohort Start Date", "2026-09-01"):
        frappe.get_doc({"doctype": "Cohort Start Date",
                        "start_date": "2026-09-01"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Cohort", "Online"):
        frappe.get_doc({"doctype": "Cohort", "cohort_name": "Online", "mode": "Online",
                        "center": "Self sign-up"}).insert(ignore_permissions=True)
    if not frappe.db.get_value("Cohort", "Online", "invite_code"):
        # a fresh RANDOM invite code per deployment — never a known default; unambiguous
        # alphabet (no 0/O/1/I/L) so it survives being read aloud or written on a slate
        import secrets
        code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
        frappe.db.set_value("Cohort", "Online", "invite_code", code, update_modified=False)
    if not frappe.db.exists("Cohort", "NGHS Sept-2026"):
        frappe.get_doc({"doctype": "Cohort", "cohort_name": "NGHS Sept-2026", "mode": "Offline",
                        "start_date": "2026-09-01",
                        "center": "Noor Girls High School"}).insert(ignore_permissions=True)

    # online students log in by USERNAME + numeric PIN (mirrors patch v2)
    ss = frappe.get_single("System Settings")
    if not ss.allow_login_using_user_name or ss.enable_password_policy:
        ss.allow_login_using_user_name = 1
        ss.enable_password_policy = 0
        ss.flags.ignore_mandatory = True
        ss.save(ignore_permissions=True)
    frappe.db.commit()
    print("=== operational defaults ready (campus, cohorts, invite code, login settings) ===")


def wipe_demo_data():
    """Production-cutover reset: erase ALL learner data — students (and their linked
    Website Users), attempts, doubts, learning events, evaluations, AI chats — while
    keeping content, milestones, cohorts, campuses and settings untouched."""
    for dt in ("AI Conversation Turn", "AI Conversation", "Learning Event",
               "Lesson Doubt", "Lesson Attempt", "Evaluation"):
        if frappe.db.exists("DocType", dt):
            frappe.db.delete(dt)
    users = [u for u in frappe.get_all("Student", filters={"user": ("!=", "")}, pluck="user") if u]
    frappe.db.delete("Student")
    frappe.db.commit()
    for u in users:                    # synthetic *.hikmat.invalid Website Users ride along
        if frappe.db.exists("User", u):
            frappe.delete_doc("User", u, force=1, ignore_permissions=True, delete_permanently=True)
    frappe.db.commit()
    print("=== learner data wiped (content/config kept):",
          frappe.db.count("Student"), "students remain ===")


def setup_trouble_report():
    """THE teaching-triage report: every lesson-activity ranked by how much students
    struggle with it — success rate, failed attempts, wrong answers, doubts, time
    spent, mid-activity bail-outs — worst first. This is the 'which lesson do I fix /
    re-teach?' list."""
    name = "Lesson Trouble Spots"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        a.track    AS "Track::110",
        a.lesson   AS "Lesson::110",
        a.activity AS "Activity::100",
        COUNT(*)                                                   AS "Attempts:Int:80",
        COUNT(DISTINCT a.student)                                  AS "Learners:Int:80",
        ROUND(100 * AVG(CASE WHEN a.total > 0
                             THEN a.score / a.total END))          AS "Success %%:Int:90",
        SUM(CASE WHEN a.stars = 0 THEN 1 ELSE 0 END)               AS "Failed (0★):Int:100",
        (SELECT COUNT(*) FROM `tabLearning Event` e
          WHERE e.kind='wrong_answer' AND e.track=a.track
            AND e.lesson=a.lesson AND e.activity=a.activity)       AS "Wrong Answers:Int:120",
        (SELECT COUNT(*) FROM `tabLesson Doubt` d
          WHERE d.track=a.track AND d.lesson=a.lesson
            AND d.activity=a.activity)                             AS "Doubts:Int:80",
        ROUND(AVG(NULLIF(a.duration_secs, 0)) / 60, 1)             AS "Avg mins:Float:90",
        (SELECT COUNT(*) FROM `tabLearning Event` e
          WHERE e.kind='dwell' AND e.track=a.track
            AND e.lesson=a.lesson AND e.activity=a.activity)       AS "Bail-outs:Int:90",
        MAX(a.attempted_on)                                        AS "Last Played:Datetime:150"
    FROM `tabLesson Attempt` a
    GROUP BY a.track, a.lesson, a.activity
    ORDER BY ROUND(100 * AVG(CASE WHEN a.total > 0 THEN a.score / a.total END)) ASC,
             SUM(CASE WHEN a.stars = 0 THEN 1 ELSE 0 END) DESC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Lesson Attempt",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Lesson Trouble Spots' ready ===")


def setup_hard_questions_report():
    """Question-level drill-down: the exact questions students get wrong, how many
    girls, and WHICH wrong answer they pick most (a shared wrong pick usually means a
    misleading distractor or a concept that needs re-teaching, not a careless slip)."""
    name = "Hardest Questions"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        e.question AS "Question::260",
        e.track    AS "Track::100",
        e.lesson   AS "Lesson::100",
        e.activity AS "Activity::90",
        COUNT(*)                       AS "Times Wrong:Int:100",
        COUNT(DISTINCT e.student)      AS "Learners:Int:80",
        (SELECT e2.chosen FROM `tabLearning Event` e2
          WHERE e2.kind='wrong_answer' AND e2.question=e.question
            AND e2.track=e.track AND e2.lesson=e.lesson AND e2.activity=e.activity
          GROUP BY e2.chosen ORDER BY COUNT(*) DESC LIMIT 1) AS "Most-picked Wrong::170",
        MAX(e.answer)                  AS "Correct Answer::150",
        MAX(e.occurred_on)             AS "Last Seen:Datetime:150"
    FROM `tabLearning Event` e
    WHERE e.kind = 'wrong_answer'
    GROUP BY e.track, e.lesson, e.activity, e.question
    ORDER BY COUNT(*) DESC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Learning Event",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Hardest Questions' ready ===")


def setup_engagement_report():
    """HOW each girl is learning, not just how well: minutes in the game (finished
    attempts + abandoned tries), replays (self-driven practice), listen taps (audio
    reliance — expected for a non-reader, a flag for a reader), language switches +
    Hindi-guide taps (how often she reaches for Hindi support — a girl leaning hard
    on Hindi may need more English scaffolding), mid-activity bail-outs (frustration),
    doubts, and when she was last seen. The 'who needs me this week?' list — most
    recently active first, so the idle girls sink visibly."""
    name = "Student Engagement"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        s.name           AS "Student:Link/Student:130",
        s.student_name   AS "Name::130",
        s.cohort         AS "Cohort::110",
        COUNT(a.name)                                      AS "Attempts:Int:80",
        COUNT(DISTINCT CONCAT(a.track, '/', a.lesson))     AS "Lessons:Int:80",
        ROUND(AVG(a.stars), 2)                             AS "Avg Stars:Float:85",
        ROUND((COALESCE(SUM(a.duration_secs), 0)
             + (SELECT COALESCE(SUM(e.duration_secs), 0) FROM `tabLearning Event` e
                 WHERE e.student = s.name AND e.kind = 'dwell')) / 60)
                                                           AS "Minutes:Int:80",
        (SELECT COALESCE(SUM(e.count), 0) FROM `tabLearning Event` e
          WHERE e.student = s.name AND e.kind = 'tool_use'
            AND e.tool = 'replay')                         AS "Replays:Int:80",
        (SELECT COALESCE(SUM(e.count), 0) FROM `tabLearning Event` e
          WHERE e.student = s.name AND e.kind = 'tool_use'
            AND e.tool IN ('listen_word','hear_screen','hear_again','hear_slow','hear_hindi'))
                                                           AS "Listen Taps:Int:95",
        (SELECT COALESCE(SUM(e.count), 0) FROM `tabLearning Event` e
          WHERE e.student = s.name AND e.kind = 'tool_use'
            AND e.tool = 'lang_switch')                    AS "Lang Switches:Int:110",
        (SELECT COALESCE(SUM(e.count), 0) FROM `tabLearning Event` e
          WHERE e.student = s.name AND e.kind = 'tool_use'
            AND e.tool = 'hindi_guide')                    AS "Hindi Guide:Int:105",
        (SELECT COUNT(*) FROM `tabLearning Event` e
          WHERE e.student = s.name AND e.kind = 'dwell')   AS "Bail-outs:Int:85",
        (SELECT COUNT(*) FROM `tabLesson Doubt` d
          WHERE d.student = s.name)                        AS "Doubts:Int:75",
        MAX(a.attempted_on)                                AS "Last Active:Datetime:150"
    FROM `tabStudent` s
    LEFT JOIN `tabLesson Attempt` a ON a.student = s.name
    WHERE s.active = 1
    GROUP BY s.name, s.student_name, s.cohort
    ORDER BY MAX(a.attempted_on) DESC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Student",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Student Engagement' ready ===")


def setup_drilldown_report():
    """Register the 'Activity Drill-down' SCRIPT report (code lives in
    hikmat/hikmat/report/activity_drill_down/) — the expandable per-student view
    behind every dashboard bar: filter by track/lesson/activity/student."""
    name = "Activity Drill-down"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Lesson Attempt",
        "report_type": "Script Report", "is_standard": "Yes", "module": MODULE,
        "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Activity Drill-down' ready ===")


def setup_attendance_reports():
    """Register the two attendance SCRIPT reports (code lives in
    hikmat/hikmat/report/daily_attendance/ and attendance_summary/):
    Daily Attendance = one row per student per day with a Present/Absent mark
    against the 2.5h threshold; Attendance Summary = one row per student over
    a range. Facilitator-only — the game never shows attendance to students."""
    for name in ("Daily Attendance", "Attendance Summary"):
        if frappe.db.exists("Report", name):
            frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
        frappe.get_doc({
            "doctype": "Report", "report_name": name, "ref_doctype": "Attendance Day",
            "report_type": "Script Report", "is_standard": "Yes", "module": MODULE,
            "roles": [{"role": "System Manager"}],
        }).insert(ignore_permissions=1)
        frappe.db.commit()
        print(f"=== report '{name}' ready ===")


def export_offline_curriculum():
    """Write the live get_courses() payload to public/curriculum.json — the static, SW-precached
    offline baseline the PWA falls back to on a first-ever-offline launch or after a localStorage
    wipe. Run after content changes and commit the file:
        bench --site hikmat.local execute hikmat.setup_data.export_offline_curriculum
    """
    import os
    from hikmat.api import _build_courses
    path = frappe.get_app_path("hikmat", "public", "curriculum.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_build_courses(), f, ensure_ascii=False, separators=(",", ":"))
    print("=== wrote", path, "===")


# ---------------------------------------------------------------------------
# Seed content — the prototype's curriculum, so the API returns the same data
# ---------------------------------------------------------------------------
COURSES = [
    {
        "key": "bazaar", "title": "The Bazaar", "titleHi": "बाज़ार", "icon": "🛒", "color": "#ff7a45",
        "blurb": "Food & market words", "blurbHi": "खाने और बाज़ार के शब्द", "published": True,
        "lessons": [
            {"key": "fruits", "title": "Fruits & Drinks", "titleHi": "फल और पेय",
             "words": [
                 {"en": "apple", "hi": "सेब", "pron": "ऐप्पल", "emoji": "🍎", "plural": "apples"},
                 {"en": "banana", "hi": "केला", "pron": "बनाना", "emoji": "🍌", "plural": "bananas"},
                 {"en": "milk", "hi": "दूध", "pron": "मिल्क", "emoji": "🥛", "uncount": True},
                 {"en": "egg", "hi": "अंडा", "pron": "एग", "emoji": "🥚", "plural": "eggs"},
                 {"en": "tea", "hi": "चाय", "pron": "टी", "emoji": "🍵", "uncount": True},
             ],
             "dialogues": [
                 {"who": "🧑‍🦱", "line": "Hello! What do you want to buy?", "lineHi": "नमस्ते! आप क्या खरीदना चाहती हैं?",
                  "replies": [{"text": "I want some milk, please.", "ok": True}, {"text": "Goodbye, see you!", "ok": False}, {"text": "I am ten years old.", "ok": False}],
                  "then": "Sure! Here is the milk."},
                 {"who": "🧑‍🦱", "line": "Do you want anything else?", "lineHi": "क्या आपको और कुछ चाहिए?",
                  "replies": [{"text": "No, that is all. Thank you.", "ok": True}, {"text": "I am hungry now.", "ok": False}, {"text": "It is raining.", "ok": False}],
                  "then": "Okay! Have a nice day."},
             ]},
            {"key": "veg", "title": "Veg & Staples", "titleHi": "सब्ज़ी और अनाज",
             "words": [
                 {"en": "tomato", "hi": "टमाटर", "pron": "टमैटो", "emoji": "🍅", "plural": "tomatoes"},
                 {"en": "potato", "hi": "आलू", "pron": "पटैटो", "emoji": "🥔", "plural": "potatoes"},
                 {"en": "rice", "hi": "चावल", "pron": "राइस", "emoji": "🍚", "uncount": True},
                 {"en": "bread", "hi": "रोटी", "pron": "ब्रेड", "emoji": "🍞", "uncount": True},
                 {"en": "fish", "hi": "मछली", "pron": "फ़िश", "emoji": "🐟", "plural": "fish"},
             ],
             "dialogues": [
                 {"who": "🧑‍🦱", "line": "That will be fifty rupees.", "lineHi": "यह पचास रुपये का होगा।",
                  "replies": [{"text": "Here is the money. Thank you.", "ok": True}, {"text": "What is your name?", "ok": False}, {"text": "I like blue.", "ok": False}],
                  "then": "Thank you. Come again!"},
                 {"who": "🧑‍🦱", "line": "Good morning! How are you?", "lineHi": "सुप्रभात! आप कैसी हैं?",
                  "replies": [{"text": "I am fine, thank you.", "ok": True}, {"text": "Two apples.", "ok": False}, {"text": "It is fifty rupees.", "ok": False}],
                  "then": "Glad to hear it!"},
             ]},
        ],
    },
    {
        "key": "home", "title": "At Home", "titleHi": "घर पर", "icon": "🏠", "color": "#6c5ce7",
        "blurb": "Everyday home words", "blurbHi": "घर के रोज़ के शब्द", "published": True,
        "lessons": [
            {"key": "house", "title": "Around the House", "titleHi": "घर के आस-पास",
             "words": [
                 {"en": "door", "hi": "दरवाज़ा", "pron": "डोर", "emoji": "🚪", "plural": "doors"},
                 {"en": "bed", "hi": "बिस्तर", "pron": "बेड", "emoji": "🛏️", "plural": "beds"},
                 {"en": "book", "hi": "किताब", "pron": "बुक", "emoji": "📖", "plural": "books"},
                 {"en": "water", "hi": "पानी", "pron": "वॉटर", "emoji": "💧", "uncount": True},
                 {"en": "clock", "hi": "घड़ी", "pron": "क्लॉक", "emoji": "🕐", "plural": "clocks"},
             ],
             "dialogues": [
                 {"who": "👩", "line": "Where is your book?", "lineHi": "तुम्हारी किताब कहाँ है?",
                  "replies": [{"text": "It is on the bed.", "ok": True}, {"text": "I am fine, thank you.", "ok": False}, {"text": "Two rupees.", "ok": False}],
                  "then": "Good. Please bring it here."},
                 {"who": "👩", "line": "Please close the door.", "lineHi": "कृपया दरवाज़ा बंद करो।",
                  "replies": [{"text": "Okay, I will close it.", "ok": True}, {"text": "I want two apples.", "ok": False}, {"text": "It is raining.", "ok": False}],
                  "then": "Thank you!"},
             ]},
        ],
    },
    {
        "key": "school", "title": "At School", "titleHi": "स्कूल में", "icon": "🏫", "color": "#2ec27e",
        "blurb": "Classroom words", "blurbHi": "कक्षा के शब्द", "published": True,
        "lessons": [
            {"key": "class", "title": "In the Classroom", "titleHi": "कक्षा में",
             "words": [
                 {"en": "pen", "hi": "कलम", "pron": "पेन", "emoji": "🖊️", "plural": "pens"},
                 {"en": "book", "hi": "किताब", "pron": "बुक", "emoji": "📚", "plural": "books"},
                 {"en": "bag", "hi": "बस्ता", "pron": "बैग", "emoji": "🎒", "plural": "bags"},
                 {"en": "chair", "hi": "कुर्सी", "pron": "चेयर", "emoji": "🪑", "plural": "chairs"},
                 {"en": "board", "hi": "बोर्ड", "pron": "बोर्ड", "emoji": "📋", "plural": "boards"},
             ],
             "dialogues": [
                 {"who": "👩‍🏫", "line": "Good morning, students!", "lineHi": "सुप्रभात, बच्चों!",
                  "replies": [{"text": "Good morning, teacher.", "ok": True}, {"text": "It is fifty rupees.", "ok": False}, {"text": "I want some rice.", "ok": False}],
                  "then": "Please sit down."},
                 {"who": "👩‍🏫", "line": "Open your book, please.", "lineHi": "अपनी किताब खोलो।",
                  "replies": [{"text": "Yes, teacher.", "ok": True}, {"text": "Goodbye!", "ok": False}, {"text": "I am hungry.", "ok": False}],
                  "then": "Let us begin."},
             ]},
        ],
    },
    {
        "key": "work", "title": "At Work", "titleHi": "काम पर", "icon": "🧰", "color": "#e0a800",
        "blurb": "Coming soon", "blurbHi": "जल्द आ रहा है", "published": False, "lessons": [],
    },
]


def _load_curriculum():
    """Full curriculum from data/curriculum.json if present, else the inline starter set."""
    import os
    path = frappe.get_app_path("hikmat", "data", "curriculum.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return COURSES


def seed_content():
    courses = _load_curriculum()
    seed_structure()   # bands + subjects must exist before tracks Link to them
    # clean slate (dev): remove existing curriculum docs, then recreate
    # (prompts Link to Lesson, so they go first)
    for dt in ["Dialect Prompt", "Dialogue", "Lesson", "Track"]:
        for n in frappe.get_all(dt, pluck="name"):
            frappe.delete_doc(dt, n, force=1, ignore_permissions=1)

    for ti, c in enumerate(courses):
        track = frappe.get_doc({
            "doctype": "Track", "track_key": c["key"], "title": c["title"], "title_hi": c["titleHi"],
            "icon": c["icon"], "color": c["color"], "blurb": c["blurb"], "blurb_hi": c["blurbHi"],
            "band": c.get("band") or None, "subject": c.get("subject") or None,
            "published": 1 if c["published"] else 0, "sort_order": ti,
        }).insert(ignore_permissions=1)

        for li, les in enumerate(c.get("lessons", [])):
            lesson = frappe.get_doc({
                "doctype": "Lesson", "track": track.name, "lesson_key": les["key"],
                "title": les["title"], "title_hi": les["titleHi"], "sort_order": li, "published": 1,
                "video": les.get("videoUrl", ""), "video_title": les.get("videoTitle", ""),
                "video_title_hi": les.get("videoTitleHi", ""), "video_duration_secs": les.get("videoDuration") or 0,
                "words": [{
                    "en": w["en"], "hi": w["hi"], "pron": w["pron"], "emoji": w["emoji"],
                    "word_type": w.get("type", ""),
                    "uncountable": 1 if w.get("uncount") else 0, "plural": w.get("plural", ""),
                    "use_en": w.get("use", ""), "use_hi": w.get("useHi", ""),
                } for w in les.get("words", [])],
                "code": [{
                    "prompt": c["prompt"], "prompt_hi": c.get("promptHi", ""),
                    "teach": c.get("teach", ""), "teach_hi": c.get("teachHi", ""),
                    "code": "\n".join(c["lines"]) if c.get("lines") else c.get("code", ""),
                    "choices": "\n".join(c["choices"]), "answer": c["answer"],
                } for c in les.get("code", [])],
                "fix": [{
                    "sentence": x["sentence"], "wrong_word": x["wrongWord"], "correction": x["fix"],
                    "teach": x.get("teach", ""), "teach_hi": x.get("teachHi", ""),
                } for x in les.get("fix", [])],
                "email": [{
                    "scenario": e["scenario"], "scenario_hi": e.get("scenarioHi", ""),
                    "spec_json": json.dumps({"to": e["to"], "from": e["from"], "slots": e["slots"]}, ensure_ascii=False),
                } for e in les.get("email", [])],
                "quiz": [{
                    "question": q["q"], "question_hi": q.get("qHi", ""), "emoji": q.get("emoji", ""),
                    "choices": "\n".join(q["choices"]), "answer": q["answer"],
                    "teach": q.get("teach", ""), "teach_hi": q.get("teachHi", ""),
                } for q in les.get("quiz", [])],
                "read": [{
                    "title": r.get("title", ""), "title_hi": r.get("titleHi", ""), "emoji": r.get("emoji", ""),
                    "passage": r["text"], "passage_hi": r.get("textHi", ""),
                    "question": r["q"], "question_hi": r.get("qHi", ""),
                    "choices": "\n".join(r["choices"]), "answer": r["answer"],
                    "teach": r.get("teach", ""), "teach_hi": r.get("teachHi", ""),
                } for r in les.get("read", [])],
                "reply": [{
                    "from_name": e["from"], "subject": e.get("subject", ""),
                    "message": e["msg"], "message_hi": e.get("msgHi", ""),
                    "spec_json": json.dumps({"slots": e["slots"]}, ensure_ascii=False),
                } for e in les.get("reply", [])],
            }).insert(ignore_permissions=1)

            for di, dl in enumerate(les.get("dialogues", [])):
                frappe.get_doc({
                    "doctype": "Dialogue", "lesson": lesson.name, "who": dl["who"],
                    "line": dl["line"], "line_hi": dl["lineHi"], "followup": dl["then"], "sort_order": di,
                    "replies": [{"text": r["text"], "text_hi": r.get("textHi", ""),
                                 "is_correct": 1 if r["ok"] else 0} for r in dl["replies"]],
                }).insert(ignore_permissions=1)

            for ci, cp in enumerate(les.get("capture", [])):
                frappe.get_doc({
                    "doctype": "Dialect Prompt", "lesson": lesson.name, "prompt_key": cp["key"],
                    "prompt_text_hi": cp["hi"], "prompt_text_en": cp.get("en", ""),
                    "category": cp.get("category", ""), "complexity_tier": cp.get("tier", 1),
                    "sort_order": ci,
                }).insert(ignore_permissions=1)

    # seed the single settings doc
    s = frappe.get_single("Hikmat Settings")
    if not s.app_name:
        s.app_name = "Hikmat"
        s.tagline_en = "Learn English by playing"
        s.tagline_hi = "खेल-खेल में अंग्रेज़ी सीखो"
        s.default_language = "en"
        s.save(ignore_permissions=1)

    frappe.db.commit()
    try:
        from hikmat.api import clear_content_cache
        clear_content_cache()
    except Exception:
        pass
    print("=== seeded", len(courses), "tracks ===")


def demo_students():
    """A small demo roster for testing the student login."""
    if not frappe.db.exists("Cohort Start Date", "2026-09-01"):
        frappe.get_doc({"doctype": "Cohort Start Date",
                        "start_date": "2026-09-01"}).insert(ignore_permissions=True)
    coh = frappe.db.exists("Cohort", "NGHS Sept-2026")
    if not coh:
        coh = frappe.get_doc({"doctype": "Cohort", "cohort_name": "NGHS Sept-2026",
                              "mode": "Offline", "start_date": "2026-09-01",
                              "center": "Noor Girls High School",
                              "facilitator": "Asha Devi"}).insert(ignore_permissions=True).name
    roster = [("Asha", 13, "👧", ""), ("Priya", 14, "🧒", ""),
              ("Sunita", 13, "👩", "1234"), ("Rekha", 15, "🙂", "")]
    for nm, age, av, pin in roster:
        if not frappe.db.exists("Student", {"student_name": nm, "cohort": coh}):
            frappe.get_doc({"doctype": "Student", "student_name": nm, "age": age, "gender": "Female",
                            "cohort": coh, "avatar": av, "login_pin": pin, "active": 1}).insert(ignore_permissions=True)
    frappe.db.commit()
    print("=== demo students ready in 'NGHS Sept-2026' (Sunita has PIN 1234) ===")


def single_center(keep="NGHS Sept-2026"):
    """Collapse to one centre — remove all other cohorts, their students & attempts."""
    for c in frappe.get_all("Cohort", pluck="name"):
        if c == keep:
            continue
        for stu in frappe.get_all("Student", filters={"cohort": c}, pluck="name"):
            for att in frappe.get_all("Lesson Attempt", filters={"student": stu}, pluck="name"):
                frappe.delete_doc("Lesson Attempt", att, force=1, ignore_permissions=1)
            frappe.delete_doc("Student", stu, force=1, ignore_permissions=1)
        frappe.delete_doc("Cohort", c, force=1, ignore_permissions=1)
    frappe.db.commit()
    print("=== kept only centre:", keep, "===")


# ---------------------------------------------------------------------------
# Analytics — denormalised fields, demo attempts, and the teacher dashboard
# ---------------------------------------------------------------------------
def add_attempt_fields():
    """Add student_name + cohort to Lesson Attempt so charts can group readably."""
    dt = frappe.get_doc("DocType", "Lesson Attempt")
    have = [f.fieldname for f in dt.fields]
    if "student_name" not in have:
        dt.append("fields", {"fieldname": "student_name", "fieldtype": "Data", "label": "Student name", "in_list_view": 1})
    if "cohort" not in have:
        dt.append("fields", {"fieldname": "cohort", "fieldtype": "Data", "label": "Cohort", "in_list_view": 1})
    if "client_id" not in have:
        # client-generated id so a retry after a partial success can't double-insert an attempt
        dt.append("fields", {"fieldname": "client_id", "fieldtype": "Data", "label": "Client id", "unique": 1, "no_copy": 1})
    dt.save()
    frappe.db.commit()
    print("=== Lesson Attempt: student_name + cohort + client_id added ===")


def demo_attempts():
    """A deterministic spread of attempts so the dashboard has something to show."""
    for n in frappe.get_all("Lesson Attempt", pluck="name"):
        frappe.delete_doc("Lesson Attempt", n, force=1, ignore_permissions=1)
    by_name = {s.student_name: s for s in frappe.get_all("Student", filters={"active": 1},
                                                         fields=["name", "student_name", "cohort"])}
    plan = {
        "Asha":   [("bazaar", "fruits", "learn", 3), ("bazaar", "fruits", "listen", 3),
                   ("bazaar", "fruits", "spell", 2), ("home", "house", "learn", 3)],
        "Priya":  [("bazaar", "fruits", "learn", 3), ("bazaar", "fruits", "listen", 2),
                   ("school", "class", "learn", 3)],
        "Rekha":  [("bazaar", "fruits", "learn", 2), ("home", "house", "learn", 1)],
        "Sunita": [("school", "class", "learn", 3), ("school", "class", "listen", 3),
                   ("school", "class", "spell", 3)],
    }
    i = 0
    for nm, rows in plan.items():
        s = by_name.get(nm)
        if not s:
            continue
        for (tk, lk, act, stars) in rows:
            frappe.get_doc({
                "doctype": "Lesson Attempt", "student": s.name, "student_name": s.student_name,
                "cohort": s.cohort, "track": tk, "lesson": lk, "activity": act,
                "stars": stars, "score": stars + 2, "total": 5, "coins": stars * 10 + (stars + 2) * 5,
                "attempted_on": frappe.utils.add_to_date(frappe.utils.now(), days=-(i % 7)),
            }).insert(ignore_permissions=1)
            i += 1
    frappe.db.commit()
    print("=== created", i, "demo attempts ===")


def _chart(name, **kw):
    if frappe.db.exists("Dashboard Chart", name):
        frappe.delete_doc("Dashboard Chart", name, force=1, ignore_permissions=1)
    base = {"doctype": "Dashboard Chart", "chart_name": name, "is_public": 1,
            "document_type": "Lesson Attempt", "color": "#6c5ce7", "filters_json": "[]"}
    base.update(kw)
    doc = frappe.get_doc(base).insert(ignore_permissions=1)
    # Frappe auto-fills currency=INR (system currency) → tooltips show ₹. Clear it so
    # counts/averages format as plain numbers.
    if doc.currency:
        doc.db_set("currency", "", update_modified=False)
    return doc.name


def _card(name, **kw):
    if frappe.db.exists("Number Card", name):
        frappe.delete_doc("Number Card", name, force=1, ignore_permissions=1)
    base = {"doctype": "Number Card", "label": name, "is_public": 1,
            "type": "Document Type", "document_type": "Lesson Attempt", "filters_json": "[]"}
    base.update(kw)
    return frappe.get_doc(base).insert(ignore_permissions=1).name


def setup_student_report():
    """A student-wise Query Report: one row per student with their progress totals."""
    name = "Student Progress"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        la.student_name   AS "Name::140",
        la.cohort         AS "Cohort::130",
        COUNT(*)                                           AS "Attempts:Int:90",
        SUM(CASE WHEN la.stars >= 1 THEN 1 ELSE 0 END)     AS "Passed:Int:80",
        COUNT(DISTINCT CONCAT(la.track, '/', la.lesson))   AS "Lessons:Int:90",
        ROUND(AVG(la.stars), 2)                            AS "Avg Stars:Float:95",
        SUM(la.coins)                                      AS "Coins:Int:90",
        DATE_FORMAT(MAX(la.attempted_on), '%%d-%%m-%%y %%H:%%i') AS "Last Active::130"
    FROM `tabLesson Attempt` la
    GROUP BY la.student, la.student_name, la.cohort
    ORDER BY COUNT(*) DESC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Lesson Attempt",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Student Progress' ready ===")


def setup_doubt_report():
    """The facilitator CONFUSION HEATMAP: which lessons/activities make learners tap
    'Roshni, mujhe doubt hai' the most — so a teacher knows where to step in. Sorted by
    doubt volume, so the hottest spots float to the top."""
    name = "Confusion Heatmap"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        d.track          AS "Track::130",
        d.lesson         AS "Lesson::130",
        d.activity       AS "Activity::120",
        COUNT(*)                                              AS "Doubts:Int:90",
        COUNT(DISTINCT d.student)                             AS "Learners:Int:90",
        SUM(CASE WHEN d.resolved = 0 THEN 1 ELSE 0 END)       AS "Open:Int:80",
        MAX(d.raised_on)                                      AS "Last Raised:Datetime:160"
    FROM `tabLesson Doubt` d
    GROUP BY d.track, d.lesson, d.activity
    ORDER BY COUNT(*) DESC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "Lesson Doubt",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'Confusion Heatmap' ready ===")


def setup_ai_report():
    """Facilitator REVIEW QUEUE for Roshni-AI: flagged + unreviewed conversations float to
    the top, then most recent. Opens the conversation to read the (Desk-only) transcript."""
    name = "AI Review Queue"
    if frappe.db.exists("Report", name):
        frappe.delete_doc("Report", name, force=1, ignore_permissions=1)
    query = """SELECT
        c.name           AS "Conversation:Link/AI Conversation:150",
        c.student_name   AS "Name::130",
        c.cohort         AS "Cohort::120",
        c.lesson         AS "Lesson::110",
        c.flagged        AS "Flagged:Check:70",
        c.flag_reason    AS "Reason::110",
        c.reviewed       AS "Reviewed:Check:80",
        c.started_on     AS "When:Datetime:160"
    FROM `tabAI Conversation` c
    ORDER BY c.flagged DESC, c.reviewed ASC, c.started_on DESC"""
    frappe.get_doc({
        "doctype": "Report", "report_name": name, "ref_doctype": "AI Conversation",
        "report_type": "Query Report", "is_standard": "No", "module": MODULE,
        "query": query, "roles": [{"role": "System Manager"}],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== report 'AI Review Queue' ready ===")


def setup_analytics():
    charts = [
        _chart("Attempts Over Time", chart_type="Count", based_on="attempted_on",
               timeseries=1, time_interval="Daily", timespan="Last Month", type="Line"),
        _chart("Attempts by Track", chart_type="Group By", group_by_based_on="track",
               group_by_type="Count", type="Bar"),
        _chart("Average Stars by Activity", chart_type="Group By", group_by_based_on="activity",
               group_by_type="Average", aggregate_function_based_on="stars", type="Bar"),
        _chart("Attempts by Student", chart_type="Group By", group_by_based_on="student_name",
               group_by_type="Count", type="Bar"),
        _chart("Attempts by Cohort", chart_type="Group By", group_by_based_on="cohort",
               group_by_type="Count", type="Bar"),
    ]
    cards = [
        _card("Total Attempts", function="Count"),
        _card("Activities Passed", function="Count",
              filters_json=json.dumps([["Lesson Attempt", "stars", ">=", 1]])),
        _card("Average Stars", type="Custom", method="hikmat.api.average_stars"),
        _card("Active Students", type="Custom", method="hikmat.api.active_student_count"),
        _card("Students Enrolled", document_type="Student", function="Count",
              filters_json=json.dumps([["Student", "active", "=", 1]])),
    ]
    if frappe.db.exists("Dashboard", "Hikmat Analytics"):
        frappe.delete_doc("Dashboard", "Hikmat Analytics", force=1, ignore_permissions=1)
    frappe.get_doc({
        "doctype": "Dashboard", "dashboard_name": "Hikmat Analytics", "is_default": 1,
        "cards": [{"card": c} for c in cards],
        "charts": [{"chart": ch} for ch in charts],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== dashboard 'Hikmat Analytics' ready:", len(charts), "charts,", len(cards), "cards ===")
    setup_student_report()
    setup_doubt_report()
    setup_ai_report()
    setup_evaluation_report()
    setup_trouble_report()
    setup_hard_questions_report()
    setup_engagement_report()
    setup_drilldown_report()
    setup_attendance_reports()

    # confusion chart — doubts grouped by lesson (the heatmap, visualised)
    if frappe.db.exists("Dashboard Chart", "Doubts by Lesson"):
        frappe.delete_doc("Dashboard Chart", "Doubts by Lesson", force=1, ignore_permissions=1)
    frappe.get_doc({
        "doctype": "Dashboard Chart", "chart_name": "Doubts by Lesson", "chart_type": "Group By",
        "document_type": "Lesson Doubt", "group_by_based_on": "lesson", "group_by_type": "Count",
        "type": "Bar", "is_public": 1, "timeseries": 0, "filters_json": "[]",
    }).insert(ignore_permissions=1)
    frappe.db.set_value("Dashboard Chart", "Doubts by Lesson", "currency", "")
    frappe.db.commit()

    setup_workspace(cards, charts)


def setup_workspace(cards=None, charts=None):
    """Backoffice landing page in Desk: stats + shortcuts to all the records."""
    cards = cards or ["Total Attempts", "Activities Passed", "Average Stars", "Active Students", "Students Enrolled"]
    charts = charts or ["Attempts Over Time", "Attempts by Track", "Average Stars by Activity",
                        "Attempts by Student", "Attempts by Cohort"]
    shortcuts = [("Lesson Trouble Spots", "Lesson Trouble Spots", "Report"),
                 ("Hardest Questions", "Hardest Questions", "Report"),
                 ("Student Engagement", "Student Engagement", "Report"),
                 ("Activity Drill-down", "Activity Drill-down", "Report"),
                 ("Student Progress", "Student Progress", "Report"),
                 ("Confusion Heatmap", "Confusion Heatmap", "Report"),
                 ("Tracks", "Track", "DocType"), ("Lessons", "Lesson", "DocType"),
                 ("Dialogues", "Dialogue", "DocType"), ("Students", "Student", "DocType"),
                 ("Cohorts", "Cohort", "DocType"), ("Attempts", "Lesson Attempt", "DocType"),
                 ("Doubts", "Lesson Doubt", "DocType"),
                 ("Pending Evaluations", "Pending Evaluations", "Report"),
                 ("Milestones", "Hikmat Milestone", "DocType"),
                 ("Daily Attendance", "Daily Attendance", "Report"),
                 ("Attendance Summary", "Attendance Summary", "Report"),
                 ("Module Tests", "Module Test", "DocType"),
                 ("Test Attempts", "Test Attempt", "DocType"),
                 ("Dialect Captures", "Dialect Capture", "DocType"),
                 ("AI Review Queue", "AI Review Queue", "Report"),
                 ("AI Chats", "AI Conversation", "DocType"),
                 ("Settings", "Hikmat Settings", "DocType")]
    # report → its ref doctype (a Report shortcut needs report_ref_doctype set)
    _report_ref = {"Student Progress": "Lesson Attempt", "Confusion Heatmap": "Lesson Doubt",
                   "AI Review Queue": "AI Conversation", "Pending Evaluations": "Evaluation",
                   "Lesson Trouble Spots": "Lesson Attempt", "Hardest Questions": "Learning Event",
                   "Student Engagement": "Student", "Activity Drill-down": "Lesson Attempt",
                   "Daily Attendance": "Attendance Day", "Attendance Summary": "Attendance Day"}

    def _sc(lbl, link, typ):
        d = {"label": lbl, "link_to": link, "type": typ}
        if typ == "Report":
            d["report_ref_doctype"] = _report_ref.get(link, "Lesson Attempt")
        return d

    def hdr(t): return {"id": "h" + str(abs(hash(t)) % 9999), "type": "header",
                        "data": {"text": '<span class="h4">' + t + "</span>", "col": 12}}
    content = [hdr("🗂️ Manage content & students")]
    content += [{"id": "sc" + str(i), "type": "shortcut", "data": {"shortcut_name": lbl, "col": 3}}
                for i, (lbl, link, typ) in enumerate(shortcuts)]
    content.append(hdr("📊 At a glance"))
    content += [{"id": "nc" + str(i), "type": "number_card", "data": {"number_card_name": c, "col": 4}}
                for i, c in enumerate(cards)]
    content.append(hdr("📈 Activity"))
    content += [{"id": "ch" + str(i), "type": "chart", "data": {"chart_name": c, "col": 6}}
                for i, c in enumerate(charts)]

    if frappe.db.exists("Workspace", "Hikmat"):
        frappe.delete_doc("Workspace", "Hikmat", force=1, ignore_permissions=1)
    frappe.get_doc({
        "doctype": "Workspace", "name": "Hikmat", "label": "Hikmat", "title": "Hikmat",
        "public": 1, "module": MODULE, "icon": "education", "content": json.dumps(content),
        "number_cards": [{"number_card_name": c, "label": c} for c in cards],
        "charts": [{"chart_name": c, "label": c} for c in charts],
        "shortcuts": [_sc(lbl, link, typ) for (lbl, link, typ) in shortcuts],
    }).insert(ignore_permissions=1)
    frappe.db.commit()
    print("=== workspace 'Hikmat' ready (", len(shortcuts), "shortcuts ) ===")
