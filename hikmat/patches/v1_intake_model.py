# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Phase-1 intake data model — seed + backfill (idempotent).

Cohort is now a start-date INTAKE batch (not a physical centre), and Student carries
mode (Campus/Online) + campus (Link Campus) + user (Link User). This:
  - seeds the physical campus (Noor Girls High School, Meghwal Mathia),
  - seeds the first real intake batch ("Aug 2026"),
  - defaults every existing student to Campus mode so the new field is never NULL.
Safe to re-run.
"""
import frappe


def execute():
    # Physical campus for the offline path (Campus ships as JSON → table exists post-migrate).
    if frappe.db.exists("DocType", "Campus") and not frappe.db.exists("Campus", "Noor Girls High School"):
        frappe.get_doc({"doctype": "Campus", "campus_name": "Noor Girls High School",
                        "location": "Meghwal Mathia", "active": 1}).insert(ignore_permissions=True)

    # First start-date intake batch.
    if not frappe.db.exists("Cohort", "Aug 2026"):
        frappe.get_doc({"doctype": "Cohort", "cohort_name": "Aug 2026",
                        "start_date": "2026-08-01"}).insert(ignore_permissions=True)

    # Every pre-existing student defaults to Campus mode (the column is NULL until set).
    if frappe.db.has_column("Student", "mode"):
        frappe.db.sql("update `tabStudent` set `mode`='Campus' where `mode` is null or `mode`=''")

    frappe.db.commit()
