# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Convert the two SQL-bodied facilitator reports into the guarded Script Reports.

`Lesson Trouble Spots` and `Student Engagement` used to be Query Reports whose body
was a SQL string stored in the Report row, so learner-authored values (`lesson`,
`activity`, `student_name`) reached the Desk grid's innerHTML and the facilitator's
spreadsheet with no escaping and no formula guard. They are now standard Script
Reports (hikmat/hikmat/report/{lesson_trouble_spots,student_engagement}/) that route
every learner value through hikmat.report_utils.

`bench migrate` normally imports a standard report's .json on its own, but only when
the file's `modified` timestamp beats the row's — a site whose rows were re-seeded
recently would silently keep the vulnerable Query Report. This patch makes the
conversion unconditional, which for a security fix is the only acceptable outcome.
Idempotent: _adopt_script_report re-imports the same on-disk definition.

Registered under [post_model_sync] in hikmat/patches.txt — without that line this module
never runs and the conversion silently does not ship. Keep it listed.
"""
import frappe

from hikmat import setup_data


def execute():
    setup_data.setup_trouble_report()
    setup_data.setup_engagement_report()
    for name in ("Lesson Trouble Spots", "Student Engagement"):
        row = frappe.db.get_value("Report", name, ["report_type", "query"], as_dict=True)
        if not row or row.report_type != "Script Report" or row.query:
            frappe.throw(f"v10: '{name}' is still a raw-SQL report: {row}")
    print("=== v10: Lesson Trouble Spots + Student Engagement now guarded script reports ===")
