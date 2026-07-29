# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Confusion Heatmap — which lessons/activities make learners tap the help button the
most, so a teacher knows where to step in. Sorted by doubt volume, hottest spot first,
with the open (unresolved) count so nothing is quietly left hanging.

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`track`, `lesson` and `activity` are posted by the game client (`hikmat.api.report_doubt`)
and leave here as "Data" columns — into the Desk grid, which assigns cell HTML directly
(frappe's Data formatter returns a Data value unchanged and frappe-datatable then does
`$cell.innerHTML = …`), and into the facilitator's CSV/XLSX, where a value opening with
`=`/`+`/`-`/`@` is evaluated as a formula. A Query Report's body is a SQL string in a DB
row: nothing in it can transform a value, and a SQL-side escape cannot tell a grid render
from an export, so it would either keep the stored XSS or corrupt the export with HTML
entities. See hikmat.report_utils.

The SQL is byte-for-byte the old query apart from the "Label:Type:width" aliases becoming
plain names plus the explicit column dicts below — identical labels, types and widths.
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    SELECT
        d.track          AS track,
        d.lesson         AS lesson,
        d.activity       AS activity,
        COUNT(*)                                              AS doubts,
        COUNT(DISTINCT d.student)                             AS learners,
        SUM(CASE WHEN d.resolved = 0 THEN 1 ELSE 0 END)       AS open_doubts,
        MAX(d.raised_on)                                      AS last_raised
    FROM `tabLesson Doubt` d
    GROUP BY d.track, d.lesson, d.activity
    ORDER BY COUNT(*) DESC
"""


def execute(filters=None):
    # `open_doubts` rather than `open`: the label a facilitator sees is still "Open", but
    # a fieldname must not collide with the datatable's own keys or shadow a builtin here.
    columns = [
        {"fieldname": "track", "label": _("Track"), "fieldtype": "Data", "width": 130},
        {"fieldname": "lesson", "label": _("Lesson"), "fieldtype": "Data", "width": 130},
        {"fieldname": "activity", "label": _("Activity"), "fieldtype": "Data", "width": 120},
        {"fieldname": "doubts", "label": _("Doubts"), "fieldtype": "Int", "width": 90},
        {"fieldname": "learners", "label": _("Learners"), "fieldtype": "Int", "width": 90},
        {"fieldname": "open_doubts", "label": _("Open"), "fieldtype": "Int", "width": 80},
        {"fieldname": "last_raised", "label": _("Last Raised"), "fieldtype": "Datetime", "width": 160},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    guard_rows(rows, "track", "lesson", "activity")
    return columns, rows
