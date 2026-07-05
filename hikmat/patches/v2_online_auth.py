# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Phase-2 online-auth site config (idempotent).

Online students are Frappe Website Users who log in by USERNAME + a numeric PIN:
  - enable login-by-username,
  - disable the site password-strength policy (a 4–8 digit PIN can't clear zxcvbn) —
    staff accounts rely on 2FA + strong-password discipline instead (see SECURITY.md).
Student logins never send email (synthetic *.hikmat.invalid addresses).
"""
import frappe


def execute():
    ss = frappe.get_single("System Settings")
    changed = False
    if not ss.allow_login_using_user_name:
        ss.allow_login_using_user_name = 1
        changed = True
    if ss.enable_password_policy:
        ss.enable_password_policy = 0
        changed = True
    if changed:
        ss.flags.ignore_mandatory = True
        ss.save(ignore_permissions=True)
        frappe.db.commit()
