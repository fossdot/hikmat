# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""AI Review Queue — the facilitator's review list for the Roshni AI tutor: flagged and
unreviewed conversations float to the top, then most recent. Each row opens the
conversation doc where the (Desk-only) transcript is read.

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`student_name`, `cohort` and `lesson` all come from the learner's device, and `flag_reason`
is stored with a length clamp but WITHOUT api._plain_text (`(flag_reason or "")[:140]`), so
it is not guaranteed markup-free either. All four leave here as "Data" columns — into the
Desk grid, which assigns cell HTML directly (frappe's Data formatter returns a Data value
unchanged; frappe-datatable then does `$cell.innerHTML = …`), and into the CSV/XLSX export,
where a leading `=`/`+`/`-`/`@` is evaluated. That matters most in THIS report: it is the
one a facilitator opens precisely because something went wrong in a conversation, i.e. the
rows most likely to be adversarial are the rows she is most likely to render.

A Query Report's body is a SQL string in a DB row — nowhere to transform a value, and a
SQL-side escape cannot tell a grid render from an export, so it would either keep the
stored XSS or entity-mangle the export. See hikmat.report_utils.

The SQL is byte-for-byte the old query apart from the "Label:Type:width" aliases becoming
plain names plus the explicit column dicts below — identical labels, types and widths.
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    SELECT
        c.name           AS conversation,
        c.student_name   AS student_name,
        c.cohort         AS cohort,
        c.lesson         AS lesson,
        c.flagged        AS flagged,
        c.flag_reason    AS flag_reason,
        c.reviewed       AS reviewed,
        c.started_on     AS started_on
    FROM `tabAI Conversation` c
    ORDER BY c.flagged DESC, c.reviewed ASC, c.started_on DESC
"""


def execute(filters=None):
    columns = [
        {"fieldname": "conversation", "label": _("Conversation"), "fieldtype": "Link",
         "options": "AI Conversation", "width": 150},
        {"fieldname": "student_name", "label": _("Name"), "fieldtype": "Data", "width": 130},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Data", "width": 120},
        {"fieldname": "lesson", "label": _("Lesson"), "fieldtype": "Data", "width": 110},
        {"fieldname": "flagged", "label": _("Flagged"), "fieldtype": "Check", "width": 70},
        {"fieldname": "flag_reason", "label": _("Reason"), "fieldtype": "Data", "width": 110},
        {"fieldname": "reviewed", "label": _("Reviewed"), "fieldtype": "Check", "width": 80},
        {"fieldname": "started_on", "label": _("When"), "fieldtype": "Datetime", "width": 160},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    # `conversation` stays raw: AI Conversation is autoname=hash, so the docname is a
    # framework-generated hash the grid renders as a link — never a byte a girl typed. The
    # client-supplied `conversation_id` is a separate field and is NOT shown here.
    guard_rows(rows, "student_name", "cohort", "lesson", "flag_reason")
    return columns, rows
