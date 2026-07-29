# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Student Progress — one row per girl with her progress totals: attempts, how many she
passed, distinct lessons touched, average stars, gems earned and when she was last
active. The plainest roll-up in the workspace, and the one a facilitator opens first.

WHY THIS IS A SCRIPT REPORT (it used to be a Query Report seeded from setup_data.py):
`student_name` is whatever a girl typed at sign-up and `cohort` is denormalised onto the
attempt row; both leave here as "Data" columns, which the Desk grid assigns with
innerHTML (frappe's Data formatter does NOT escape) and which the CSV/XLSX export writes
into a spreadsheet cell where a leading `=`/`+`/`-`/`@` is evaluated. A Query Report's
body is a SQL string in a DB row — nowhere to transform the values, and a SQL-side escape
cannot tell a grid render from a spreadsheet export, so it would either keep the stored
XSS or entity-mangle the exported Devanagari. See hikmat.report_utils.

The SQL is the old query unchanged apart from the "Label:Type:width" aliases becoming
plain names plus the explicit column dicts below (identical labels, types and widths),
and `%%d` becoming `%d`: the doubling was only needed because the string was mogrified by
pymysql, and frappe.db.sql() only mogrifies a query it is given values for.
"""
import frappe
from frappe import _

from hikmat.report_utils import guard_rows

QUERY = """
    SELECT
        la.student_name   AS student_name,
        la.cohort         AS cohort,
        COUNT(*)                                           AS attempts,
        SUM(CASE WHEN la.stars >= 1 THEN 1 ELSE 0 END)     AS passed,
        COUNT(DISTINCT CONCAT(la.track, '/', la.lesson))   AS lessons,
        ROUND(AVG(la.stars), 2)                            AS avg_stars,
        SUM(la.coins)                                      AS coins,
        DATE_FORMAT(MAX(la.attempted_on), '%d-%m-%y %H:%i') AS last_active
    FROM `tabLesson Attempt` la
    GROUP BY la.student, la.student_name, la.cohort
    ORDER BY COUNT(*) DESC
"""


def execute(filters=None):
    columns = [
        {"fieldname": "student_name", "label": _("Name"), "fieldtype": "Data", "width": 140},
        {"fieldname": "cohort", "label": _("Cohort"), "fieldtype": "Data", "width": 130},
        {"fieldname": "attempts", "label": _("Attempts"), "fieldtype": "Int", "width": 90},
        {"fieldname": "passed", "label": _("Passed"), "fieldtype": "Int", "width": 80},
        {"fieldname": "lessons", "label": _("Lessons"), "fieldtype": "Int", "width": 90},
        {"fieldname": "avg_stars", "label": _("Avg Stars"), "fieldtype": "Float", "precision": 2, "width": 95},
        {"fieldname": "coins", "label": _("Coins"), "fieldtype": "Int", "width": 90},
        {"fieldname": "last_active", "label": _("Last Active"), "fieldtype": "Data", "width": 130},
    ]
    rows = frappe.db.sql(QUERY, as_dict=True)
    # `cohort` is a plain Data column here (not a Link), so it is guarded like any other
    # free text — a guard apostrophe would only break a href the grid never renders.
    # `last_active` stays raw ON PURPOSE: it is DATE_FORMAT() over a Datetime DB column,
    # so it cannot carry a byte a girl typed, and it never leads with a formula char.
    guard_rows(rows, "student_name", "cohort")
    return columns, rows
