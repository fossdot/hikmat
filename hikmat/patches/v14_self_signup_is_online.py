# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Put self-signup learners in the Online mode they actually belong to.

Student.mode shipped with a default of "Campus", and the self-signup door
(_insert_self_signup_student) set only the *cohort* — so every learner who signed herself
up through the app landed as cohort=Online + mode=Campus. Two fields describing the same
fact, disagreeing. It was visible the moment the Play Store testers arrived: nine rows in
the Student list, all "Online" under Cohort and all "Campus" under Mode.

Campus mode means a girl physically in the programme, added by a facilitator and attached
to a Campus — twenty-six of them, entered by hand. Everyone who arrives through the app is
Online. The doctype default is now "Online" and the signup path sets mode itself; this
backfills the rows created before that.

DELIBERATELY NARROW. It only touches rows that carry the self-signup fingerprint — the
"Online" cohort AND no campus — so a real campus girl is never reassigned, including one
whose row predates the campus field or whose mode a facilitator has already corrected by
hand. A campus student always has `campus` set (the field is what the offline roster in
campus_roster filters on), so she cannot match.
"""
import frappe


def execute():
    if not frappe.db.has_column("Student", "mode"):
        return
    if not frappe.db.has_column("Student", "campus"):
        # Pre-v1 schema: no campus field to distinguish the two intakes, so there is no safe
        # narrow filter and nothing to migrate yet. v1_intake_model adds it.
        return
    frappe.db.sql("""
        update `tabStudent`
           set `mode` = 'Online'
         where `mode` = 'Campus'
           and (`campus` is null or `campus` = '')
           and `cohort` = 'Online'
    """)
    frappe.db.commit()
