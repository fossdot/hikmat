# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Consolidate to exactly two cohorts: "Online" (self-signup + invite-code path)
and "NGHS Sept-2026" (the Noor campus batch). Idempotent."""
import frappe

ONLINE, CAMPUS = "Online", "NGHS Sept-2026"
_OLD_ONLINE = ("Aug 2026", "New Learners")
_OLD_CAMPUS = ("Noor Campus Aug 2026", "Bettiah Center")
_COHORT_REF_DOCTYPES = ("Student", "Lesson Attempt", "Lesson Doubt", "Learning Event", "Evaluation")


def _move(old, new):
    for dt in _COHORT_REF_DOCTYPES:
        if frappe.db.table_exists(dt) and frappe.db.has_column(dt, "cohort"):
            frappe.db.sql("update `tab%s` set cohort=%%s where cohort=%%s" % dt, (new, old))


def execute():
    if not frappe.db.exists("Cohort Start Date", "2026-09-01"):
        frappe.get_doc({"doctype": "Cohort Start Date",
                        "start_date": "2026-09-01"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Cohort", ONLINE):
        frappe.get_doc({"doctype": "Cohort", "cohort_name": ONLINE, "mode": "Online",
                        "center": "Self sign-up"}).insert(ignore_permissions=True)
    if not frappe.db.exists("Cohort", CAMPUS):
        frappe.get_doc({"doctype": "Cohort", "cohort_name": CAMPUS, "mode": "Offline",
                        "start_date": "2026-09-01",
                        "center": "Noor Girls High School"}).insert(ignore_permissions=True)

    # the invite code moves to the Online cohort so signup_online keeps working
    for old in _OLD_ONLINE:
        if frappe.db.exists("Cohort", old):
            code = frappe.db.get_value("Cohort", old, "invite_code")
            if code and not frappe.db.get_value("Cohort", ONLINE, "invite_code"):
                frappe.db.set_value("Cohort", old, "invite_code", None, update_modified=False)
                frappe.db.set_value("Cohort", ONLINE, "invite_code", code, update_modified=False)
            _move(old, ONLINE)
            frappe.delete_doc("Cohort", old, force=1, ignore_permissions=True)
    for old in _OLD_CAMPUS:
        if frappe.db.exists("Cohort", old):
            _move(old, CAMPUS)
            frappe.delete_doc("Cohort", old, force=1, ignore_permissions=True)
    frappe.db.commit()
