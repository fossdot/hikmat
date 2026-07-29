# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Lesson Trouble Spots — THE teaching-triage report: every lesson-activity ranked by
how much students struggle with it (success rate, failed attempts, wrong answers,
doubts, time spent, mid-activity bail-outs), worst first. The "which lesson do I fix /
re-teach?" list.

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`track`, `lesson` and `activity` are supplied by the game client (`submit_attempt`),
so they are learner-authored, and they leave here as "Data" columns — straight into
the Desk grid's innerHTML and into the facilitator's CSV/XLSX. A Query Report's body
is a SQL string in a DB row: there is nowhere in it to transform the values, and a
SQL-side escape (nested REPLACE) could not tell a grid render from a spreadsheet
export, so it would either keep the XSS or corrupt the export with HTML entities.
Python `execute()` can tell them apart — see hikmat.report_utils.

The SQL below is byte-for-byte the old query except that the "Label:Type:width"
aliases became explicit column dicts (and `%%` became `%`, since nothing is
interpolated any more).
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    select a.track, a.lesson, a.activity,
           count(*)                                              as attempts,
           count(distinct a.student)                             as learners,
           round(100 * avg(case when a.total > 0
                                then a.score / a.total end))     as success,
           sum(case when a.stars = 0 then 1 else 0 end)          as failed_zero,
           (select count(*) from `tabLearning Event` e
             where e.kind='wrong_answer' and e.track=a.track
               and e.lesson=a.lesson and e.activity=a.activity)  as wrong_answers,
           (select count(*) from `tabLesson Doubt` d
             where d.track=a.track and d.lesson=a.lesson
               and d.activity=a.activity)                        as doubts,
           round(avg(nullif(a.duration_secs, 0)) / 60, 1)        as avg_mins,
           (select count(*) from `tabLearning Event` e
             where e.kind='dwell' and e.track=a.track
               and e.lesson=a.lesson and e.activity=a.activity)  as bail_outs,
           max(a.attempted_on)                                   as last_played
    from `tabLesson Attempt` a
    group by a.track, a.lesson, a.activity
    order by round(100 * avg(case when a.total > 0 then a.score / a.total end)) asc,
             sum(case when a.stars = 0 then 1 else 0 end) desc
"""


def execute(filters=None):
    columns = [
        {"fieldname": "track", "label": _("Track"), "fieldtype": "Data", "width": 110},
        {"fieldname": "lesson", "label": _("Lesson"), "fieldtype": "Data", "width": 110},
        {"fieldname": "activity", "label": _("Activity"), "fieldtype": "Data", "width": 100},
        {"fieldname": "attempts", "label": _("Attempts"), "fieldtype": "Int", "width": 80},
        {"fieldname": "learners", "label": _("Learners"), "fieldtype": "Int", "width": 80},
        {"fieldname": "success", "label": _("Success %"), "fieldtype": "Int", "width": 90},
        {"fieldname": "failed_zero", "label": _("Failed (0★)"), "fieldtype": "Int", "width": 100},
        {"fieldname": "wrong_answers", "label": _("Wrong Answers"), "fieldtype": "Int", "width": 120},
        {"fieldname": "doubts", "label": _("Doubts"), "fieldtype": "Int", "width": 80},
        {"fieldname": "avg_mins", "label": _("Avg mins"), "fieldtype": "Float", "precision": 1, "width": 90},
        {"fieldname": "bail_outs", "label": _("Bail-outs"), "fieldtype": "Int", "width": 90},
        {"fieldname": "last_played", "label": _("Last Played"), "fieldtype": "Datetime", "width": 150},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    # the whole reason this report is Python: the three client-supplied keys get the
    # destination-aware guard (escape for the grid, formula-guard for the spreadsheet)
    guard_rows(rows, "track", "lesson", "activity")
    return columns, rows
