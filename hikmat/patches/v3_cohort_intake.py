# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Cohorts split Online/Offline; start date becomes a controlled dropdown
(Cohort Start Date doctype), mandatory only for Offline batches. Idempotent."""
import frappe


def execute():
    from hikmat import setup_data
    setup_data.update_cohort_fields()
    if frappe.db.exists("DocType", "Cohort Start Date") \
            and not frappe.db.exists("Cohort Start Date", "2026-08-01"):
        frappe.get_doc({"doctype": "Cohort Start Date",
                        "start_date": "2026-08-01"}).insert(ignore_permissions=True)
    # invite-code / self-signup batches are the online path
    frappe.db.sql("""update `tabCohort` set mode='Online'
                     where name in ('Aug 2026', 'New Learners')""")
    frappe.db.sql("""update `tabCohort` set mode='Offline'
                     where mode is null or mode=''""")
    frappe.db.commit()
