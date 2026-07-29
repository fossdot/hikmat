# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Student Engagement — HOW each girl is learning, not just how well: minutes in the
game (finished attempts + abandoned tries), replays (self-driven practice), listen taps
(audio reliance — expected for a non-reader, a flag for a reader), language switches +
Hindi-guide taps (how often she reaches for Hindi support — a girl leaning hard on
Hindi may need more English scaffolding), mid-activity bail-outs (frustration), doubts,
and when she was last seen. The "who needs me this week?" list — most recently active
first, so the idle girls sink visibly.

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`student_name` is whatever a girl typed at sign-up and it leaves here as a "Data"
column, which the Desk grid assigns with innerHTML and the export writes into a
spreadsheet cell. A Query Report's body is a SQL string in a DB row — nowhere to
transform the values, and a SQL-side escape cannot tell a grid render from a CSV
export, so it would either keep the XSS or corrupt the export with HTML entities.
See hikmat.report_utils for the destination-aware guard.

The SQL below is the old query unchanged apart from the "Label:Type:width" aliases
becoming explicit column dicts.
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    select s.name                                          as student,
           s.student_name                                  as student_name,
           s.cohort                                        as cohort,
           count(a.name)                                   as attempts,
           count(distinct concat(a.track, '/', a.lesson))   as lessons,
           round(avg(a.stars), 2)                          as avg_stars,
           round((coalesce(sum(a.duration_secs), 0)
                + (select coalesce(sum(e.duration_secs), 0) from `tabLearning Event` e
                    where e.student = s.name and e.kind = 'dwell')) / 60)
                                                           as minutes,
           (select coalesce(sum(e.count), 0) from `tabLearning Event` e
             where e.student = s.name and e.kind = 'tool_use'
               and e.tool = 'replay')                      as replays,
           (select coalesce(sum(e.count), 0) from `tabLearning Event` e
             where e.student = s.name and e.kind = 'tool_use'
               and e.tool in ('listen_word','hear_screen','hear_again','hear_slow','hear_hindi'))
                                                           as listen_taps,
           (select coalesce(sum(e.count), 0) from `tabLearning Event` e
             where e.student = s.name and e.kind = 'tool_use'
               and e.tool = 'lang_switch')                 as lang_switches,
           (select coalesce(sum(e.count), 0) from `tabLearning Event` e
             where e.student = s.name and e.kind = 'tool_use'
               and e.tool = 'hindi_guide')                 as hindi_guide,
           (select count(*) from `tabLearning Event` e
             where e.student = s.name and e.kind = 'dwell') as bail_outs,
           (select count(*) from `tabLesson Doubt` d
             where d.student = s.name)                     as doubts,
           max(a.attempted_on)                             as last_active
    from `tabStudent` s
    left join `tabLesson Attempt` a on a.student = s.name
    where s.active = 1
    group by s.name, s.student_name, s.cohort
    order by max(a.attempted_on) desc
"""


def execute(filters=None):
    columns = [
        {"fieldname": "student", "label": _("Student"), "fieldtype": "Link", "options": "Student", "width": 130},
        {"fieldname": "student_name", "label": _("Name"), "fieldtype": "Data", "width": 130},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Data", "width": 110},
        {"fieldname": "attempts", "label": _("Attempts"), "fieldtype": "Int", "width": 80},
        {"fieldname": "lessons", "label": _("Lessons"), "fieldtype": "Int", "width": 80},
        {"fieldname": "avg_stars", "label": _("Avg Stars"), "fieldtype": "Float", "precision": 2, "width": 85},
        {"fieldname": "minutes", "label": _("Minutes"), "fieldtype": "Int", "width": 80},
        {"fieldname": "replays", "label": _("Replays"), "fieldtype": "Int", "width": 80},
        {"fieldname": "listen_taps", "label": _("Listen Taps"), "fieldtype": "Int", "width": 95},
        {"fieldname": "lang_switches", "label": _("Lang Switches"), "fieldtype": "Int", "width": 110},
        {"fieldname": "hindi_guide", "label": _("Hindi Guide"), "fieldtype": "Int", "width": 105},
        {"fieldname": "bail_outs", "label": _("Bail-outs"), "fieldtype": "Int", "width": 85},
        {"fieldname": "doubts", "label": _("Doubts"), "fieldtype": "Int", "width": 75},
        {"fieldname": "last_active", "label": _("Last Active"), "fieldtype": "Datetime", "width": 150},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    # `student` stays raw: it is an autoname hash the grid renders as a link, never
    # learner text. `cohort` is a plain Data column here, so it is guarded too.
    guard_rows(rows, "student_name", "cohort")
    return columns, rows
