# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Phase-0 auth hardening backfill.

Token validation is now expiry-aware (see hikmat.api._token_ok / _token_valid): a token
with no `token_issued_on` is treated as expired and rejected. Existing students who already
hold an `auth_token` from before this change have a NULL issued-on, so without a backfill
they'd be logged out until their next login. Stamp them 'now' so an active token keeps
working (subsequent logins slide the window forward). Idempotent — safe to re-run.
"""
import frappe


def execute():
    if not frappe.db.has_column("Student", "token_issued_on"):
        return
    frappe.db.sql(
        """update `tabStudent`
           set token_issued_on = %s
           where auth_token is not null and auth_token != '' and token_issued_on is null""",
        frappe.utils.now(),
    )
    frappe.db.commit()
