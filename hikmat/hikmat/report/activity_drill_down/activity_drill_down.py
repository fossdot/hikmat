# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Activity Drill-down — the expandable view behind the dashboard bars.

Every row is one (student × track × lesson × activity): how many attempts, how
well it went (avg/best stars, success %), and the struggle signals (wrong answers,
doubts). All filters are optional — no filter shows the whole class, then a
facilitator narrows to one track, one activity, or one girl.
"""
import frappe
from frappe import _


def execute(filters=None):
    filters = frappe._dict(filters or {})
    cond, vals = [], {}
    if filters.get("track"):
        cond.append("a.track = %(track)s")
        vals["track"] = filters.track
    if filters.get("lesson"):
        cond.append("a.lesson like %(lesson)s")
        vals["lesson"] = "%" + filters.lesson + "%"
    if filters.get("activity"):
        cond.append("a.activity = %(activity)s")
        vals["activity"] = filters.activity
    if filters.get("student"):
        cond.append("a.student = %(student)s")
        vals["student"] = filters.student
    if filters.get("cohort"):
        cond.append("a.cohort = %(cohort)s")
        vals["cohort"] = filters.cohort
    where = ("where " + " and ".join(cond)) if cond else ""

    rows = frappe.db.sql(f"""
        select a.student, a.student_name, a.cohort, a.track, a.lesson, a.activity,
               count(*)                                                as attempts,
               round(avg(a.stars), 2)                                  as avg_stars,
               max(a.stars)                                            as best_stars,
               round(100 * avg(case when a.total > 0
                                    then a.score / a.total end))       as success,
               sum(a.coins)                                            as gems,
               max(a.attempted_on)                                     as last_played
        from `tabLesson Attempt` a
        {where}
        group by a.student, a.cohort, a.track, a.lesson, a.activity
        order by success asc, attempts desc""", vals, as_dict=True)

    # struggle signals, joined in python (cheap at classroom scale)
    wrongs = {(w.student, w.track, w.lesson, w.activity): w.c for w in frappe.db.sql(
        """select student, track, lesson, activity, count(*) c from `tabLearning Event`
           where kind='wrong_answer' group by student, track, lesson, activity""", as_dict=True)}
    doubts = {(d.student, d.track, d.lesson, d.activity): d.c for d in frappe.db.sql(
        """select student, track, lesson, activity, count(*) c from `tabLesson Doubt`
           group by student, track, lesson, activity""", as_dict=True)}
    for r in rows:
        key = (r.student, r.track, r.lesson, r.activity)
        r.wrong_answers = wrongs.get(key, 0)
        r.doubts = doubts.get(key, 0)

    columns = [
        {"fieldname": "student", "label": _("Student"), "fieldtype": "Link", "options": "Student", "width": 120},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Link", "options": "Cohort", "width": 120},
        {"fieldname": "student_name", "label": _("Name"), "fieldtype": "Data", "width": 110},
        {"fieldname": "track", "label": _("Track"), "fieldtype": "Link", "options": "Track", "width": 100},
        {"fieldname": "lesson", "label": _("Lesson"), "fieldtype": "Data", "width": 100},
        {"fieldname": "activity", "label": _("Activity"), "fieldtype": "Data", "width": 90},
        {"fieldname": "attempts", "label": _("Attempts"), "fieldtype": "Int", "width": 90},
        {"fieldname": "avg_stars", "label": _("Avg ★"), "fieldtype": "Float", "precision": 2, "width": 80},
        {"fieldname": "best_stars", "label": _("Best ★"), "fieldtype": "Int", "width": 80},
        {"fieldname": "success", "label": _("Success %"), "fieldtype": "Int", "width": 95},
        {"fieldname": "wrong_answers", "label": _("Wrong Answers"), "fieldtype": "Int", "width": 125},
        {"fieldname": "doubts", "label": _("Doubts"), "fieldtype": "Int", "width": 80},
        {"fieldname": "gems", "label": _("Gems 💎"), "fieldtype": "Int", "width": 90},
        {"fieldname": "last_played", "label": _("Last Played"), "fieldtype": "Datetime", "width": 150},
    ]

    # chart: average stars per student over whatever is filtered
    per_student = {}
    for r in rows:
        per_student.setdefault(r.student_name or r.student, []).append(r.avg_stars or 0)
    chart = {
        "data": {
            "labels": list(per_student.keys()),
            "datasets": [{"name": _("Avg stars"),
                          "values": [round(sum(v) / len(v), 2) for v in per_student.values()]}],
        },
        "type": "bar",
        "colors": ["#6c5ce7"],
    } if per_student else None

    total_attempts = sum(r.attempts for r in rows)
    total_wrong = sum(r.wrong_answers for r in rows)
    succ = [r.success for r in rows if r.success is not None]
    report_summary = [
        {"label": _("Attempts"), "value": total_attempts, "datatype": "Int"},
        {"label": _("Avg success"), "value": (str(round(sum(succ) / len(succ))) + "%") if succ else "—", "datatype": "Data"},
        {"label": _("Wrong answers"), "value": total_wrong, "datatype": "Int",
         "indicator": "Red" if total_wrong else "Green"},
        {"label": _("Doubts"), "value": sum(r.doubts for r in rows), "datatype": "Int"},
    ]

    return columns, rows, None, chart, report_summary
