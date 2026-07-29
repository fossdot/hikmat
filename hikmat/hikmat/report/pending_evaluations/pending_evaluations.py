# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Pending Evaluations — who reached a milestone belt and is waiting for an in-person
rubric evaluation, oldest wait first so nobody is left locked out of the next track.

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`student_name` is whatever a girl typed at sign-up, denormalised onto the Evaluation row,
and it leaves here as a "Data" column — into the Desk grid, which assigns cell HTML
directly (frappe's Data formatter returns a Data value unchanged and frappe-datatable then
does `$cell.innerHTML = …`), and into the facilitator's CSV/XLSX, where a value opening
with `=`/`+`/`-`/`@` is evaluated as a formula or a DDE prompt. A Query Report's body is a
SQL string in a DB row — nowhere to transform a value, and a SQL-side escape cannot tell a
grid render from an export, so it would either keep the stored XSS or entity-mangle the
exported Devanagari. See hikmat.report_utils.

The SQL is byte-for-byte the old query apart from the "Label:Type:width" aliases becoming
plain names plus the explicit column dicts below — identical labels, types and widths, and
the same `status = 'Pending'` filter and `reached_on ASC` ordering.
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    SELECT
        e.name           AS evaluation,
        e.student_name   AS student_name,
        e.cohort         AS cohort,
        e.campus         AS campus,
        e.milestone      AS milestone,
        e.threshold_gems AS threshold_gems,
        e.gems_at_reach  AS gems_at_reach,
        e.reached_on     AS reached_on
    FROM `tabEvaluation` e
    WHERE e.status = 'Pending'
    ORDER BY e.reached_on ASC
"""


def execute(filters=None):
    columns = [
        {"fieldname": "evaluation", "label": _("Evaluation"), "fieldtype": "Link",
         "options": "Evaluation", "width": 160},
        {"fieldname": "student_name", "label": _("Student"), "fieldtype": "Data", "width": 140},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Data", "width": 120},
        {"fieldname": "campus", "label": _("Campus"), "fieldtype": "Data", "width": 140},
        {"fieldname": "milestone", "label": _("Milestone"), "fieldtype": "Data", "width": 110},
        {"fieldname": "threshold_gems", "label": _("Threshold"), "fieldtype": "Int", "width": 100},
        {"fieldname": "gems_at_reach", "label": _("Gems"), "fieldtype": "Int", "width": 90},
        {"fieldname": "reached_on", "label": _("Reached"), "fieldtype": "Datetime", "width": 160},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    # `evaluation` stays raw: it is the real docname (autoname EV-{student}-{milestone},
    # both of which are docnames themselves) and the grid renders it as the link a
    # facilitator clicks to run the evaluation — a guard apostrophe would break that href.
    # cohort / campus / milestone ARE guarded even though they are staff-created docnames:
    # they are plain Data columns here, not Links, so nothing renders an href off them and
    # a facilitator who copies the grid into a spreadsheet must not carry a live formula.
    guard_rows(rows, "student_name", "cohort", "campus", "milestone")
    return columns, rows
