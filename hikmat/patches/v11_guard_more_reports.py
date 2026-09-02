# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Convert the LAST five SQL-bodied facilitator reports into guarded Script Reports.

v10 did `Lesson Trouble Spots` and `Student Engagement`. It missed five siblings with the
same defect: `Hardest Questions`, `Student Progress`, `Confusion Heatmap`,
`AI Review Queue` and `Pending Evaluations` were still Query Reports whose body was a SQL
string in the Report row, so learner-authored text (a question, a chosen wrong answer, a
sign-up name, a cohort, an AI flag reason) reached the Desk grid as an unescaped "Data"
cell — frappe's Data formatter returns it unchanged and frappe-datatable assigns it with
`$cell.innerHTML = …`, inside a System Manager session — and reached the facilitator's
CSV/XLSX as an evaluable formula cell. They are now standard Script Reports under
hikmat/hikmat/report/ that route every learner value through hikmat.report_utils.

WHY A NEW PATCH INSTEAD OF EXTENDING v10: v10 is already recorded in the Patch Log of
every site it has run on (including this dev site), and frappe never re-runs a completed
patch. Adding these five to v10 would therefore have shipped a no-op — the reports would
have stayed vulnerable on exactly the sites that already migrated. A security fix must not
depend on a site not having run the previous one.

`bench migrate` does import a standard report's .json on its own, but only when the file's
`modified` timestamp beats the row's, and a site whose rows were re-seeded recently would
silently keep the vulnerable Query Report. This patch makes the conversion unconditional.

Idempotent: every setup_* below is a call to _adopt_script_report, which re-imports the
same on-disk definition (import_file_by_path with force=1), so a second run rewrites the
identical row and asserts the same postconditions. Registered under [post_model_sync] in
hikmat/patches.txt — without that line this module never runs and the conversion silently
does not ship. Keep it listed.
"""
import frappe

from hikmat import setup_data

# report name -> the setup_data function that now adopts the on-disk script report
CONVERTED = {
    "Hardest Questions": setup_data.setup_hard_questions_report,
    "Student Progress": setup_data.setup_student_report,
    "Confusion Heatmap": setup_data.setup_doubt_report,
    "AI Review Queue": setup_data.setup_ai_report,
    # "Pending Evaluations" was here until v17 removed the belt gates and, with them, the
    # Evaluation doctype this report read. Its guard is not weakened, it has no rows to guard.
}


def execute():
    for name, setup in CONVERTED.items():
        setup()
    # Verify, don't assume: a leftover `query` in the row means SOME other code path
    # re-seeded the raw SQL after us, and that is a live stored-XSS sink again.
    for name in CONVERTED:
        row = frappe.db.get_value("Report", name, ["report_type", "query"], as_dict=True)
        if not row or row.report_type != "Script Report" or row.query:
            frappe.throw(f"v11: '{name}' is still a raw-SQL report: {row}")
    # v10's pair too — cheap, and it turns this patch into the one place that states the
    # invariant for ALL of them, so a site that somehow skipped v10 is still fixed here.
    for name in ("Lesson Trouble Spots", "Student Engagement"):
        if frappe.db.get_value("Report", name, "query"):
            frappe.throw(f"v11: '{name}' regressed to raw SQL")
    print("=== v11:", len(CONVERTED), "more reports now guarded script reports ===")
