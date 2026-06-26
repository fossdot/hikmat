"""One-shot scrub: remove Lesson Doubt rows whose linked Student no longer exists.

Pre-fix, hikmat.api.delete_student deleted Student + Lesson Attempt rows but NOT
the matching Lesson Doubt rows. This patch cleans up doubts orphaned by those
historical erasures so a deployed site converges with the post-fix invariant.
"""
import frappe


def execute():
    orphan = frappe.db.sql_list(
        """SELECT d.name
             FROM `tabLesson Doubt` d
        LEFT JOIN `tabStudent` s ON s.name = d.student
            WHERE d.student IS NOT NULL
              AND d.student <> ''
              AND s.name IS NULL"""
    )
    for doubt in orphan:
        frappe.delete_doc("Lesson Doubt", doubt, force=1, ignore_permissions=True)
    frappe.db.commit()
    print(f"scrubbed {len(orphan)} orphan Lesson Doubt rows")
