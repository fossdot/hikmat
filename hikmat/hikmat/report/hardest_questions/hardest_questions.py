# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Hardest Questions — question-level drill-down: the exact questions students get
wrong, how many girls got each one wrong, and WHICH wrong answer they pick most (a
shared wrong pick usually means a misleading distractor or a concept that needs
re-teaching, not a careless slip).

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`question`, `chosen`, `answer`, `track`, `lesson` and `activity` are ALL posted by the
game client (`hikmat.api.log_event`), so every one of them is learner-authored, and
they leave here as "Data" columns — straight into the Desk grid's innerHTML (frappe's
Data formatter returns the value unchanged; frappe-datatable then assigns it with
`$cell.innerHTML = …`) and into the facilitator's CSV/XLSX, where a leading `=`/`+`
becomes a live formula. A Query Report's body is a SQL string in a DB row: there is
nowhere in it to transform a value, and a SQL-side escape could not tell a grid render
from a spreadsheet export, so it would either keep the stored XSS or corrupt the export
with HTML entities. Python `execute()` can tell them apart — see hikmat.report_utils.

This report has the WIDEST learner surface of all of them: six free-text columns, two
of which (`question`, `Most-picked Wrong`) are echoed straight back from whatever the
device posted.

The SQL below is byte-for-byte the old query except that the "Label:Type:width" aliases
became plain column names plus the explicit column dicts in execute() (identical labels,
types and widths), and `%%` became `%` because nothing is interpolated any more —
frappe.db.sql() only mogrifies when it is given values.
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    SELECT
        e.question AS question,
        e.track    AS track,
        e.lesson   AS lesson,
        e.activity AS activity,
        COUNT(*)                       AS times_wrong,
        COUNT(DISTINCT e.student)      AS learners,
        (SELECT e2.chosen FROM `tabLearning Event` e2
          WHERE e2.kind='wrong_answer' AND e2.question=e.question
            AND e2.track=e.track AND e2.lesson=e.lesson AND e2.activity=e.activity
          GROUP BY e2.chosen ORDER BY COUNT(*) DESC LIMIT 1) AS most_picked_wrong,
        MAX(e.answer)                  AS correct_answer,
        MAX(e.occurred_on)             AS last_seen
    FROM `tabLearning Event` e
    WHERE e.kind = 'wrong_answer'
    GROUP BY e.track, e.lesson, e.activity, e.question
    ORDER BY COUNT(*) DESC
"""

# Learner-authored columns. `question` is deliberately NOT length-capped for the grid:
# api.log_event already clamps it to 500 chars on the way in, and the facilitator's whole
# job here is to read the question that is failing.
LEARNER_FIELDS = ("question", "track", "lesson", "activity",
                  "most_picked_wrong", "correct_answer")


def execute(filters=None):
    columns = [
        {"fieldname": "question", "label": _("Question"), "fieldtype": "Data", "width": 260},
        {"fieldname": "track", "label": _("Track"), "fieldtype": "Data", "width": 100},
        {"fieldname": "lesson", "label": _("Lesson"), "fieldtype": "Data", "width": 100},
        {"fieldname": "activity", "label": _("Activity"), "fieldtype": "Data", "width": 90},
        {"fieldname": "times_wrong", "label": _("Times Wrong"), "fieldtype": "Int", "width": 100},
        {"fieldname": "learners", "label": _("Learners"), "fieldtype": "Int", "width": 80},
        {"fieldname": "most_picked_wrong", "label": _("Most-picked Wrong"), "fieldtype": "Data", "width": 170},
        {"fieldname": "correct_answer", "label": _("Correct Answer"), "fieldtype": "Data", "width": 150},
        {"fieldname": "last_seen", "label": _("Last Seen"), "fieldtype": "Datetime", "width": 150},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    # the whole reason this report is Python: every client-posted string gets the
    # destination-aware guard (escape for the grid, formula-guard for the spreadsheet)
    guard_rows(rows, *LEARNER_FIELDS)
    return columns, rows
