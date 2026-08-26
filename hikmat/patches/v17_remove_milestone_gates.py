# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Remove the milestone "belt" gates entirely.

WHAT THEY DID. Crossing a gem threshold created a Pending Evaluation and LOCKED every new
lesson until a facilitator opened Desk and marked it Passed. A girl with 79 stars and 1,420
gems was shown "Waiting for your teacher's evaluation" instead of the next lesson — reported
from a phone with a screenshot. The gate was switched off in the client first; this removes
the feature: "learners should learn unstopped" (Vishal, 2026-08-26).

WHAT GOES: the Hikmat Milestone doctype (belt definitions — pure configuration this app
seeded), the Evaluation doctype and its rows, the "Pending Evaluations" report, and the two
Desk workspace shortcuts. get_settings no longer ships thresholds, get_progress no longer
ships gate statuses, and submit_attempt no longer creates Evaluations or pings facilitators.

WHAT STAYS: gems. They were never the problem — they are the reward the app is built around,
they still accumulate on replays, and the result screen still shows what an activity earned.

ON DELETING THE DATA. Evaluation rows are machine-created: submit_attempt inserted one with
status Pending the moment a threshold was crossed. Some may since have been marked Passed by a
facilitator, and that is a human judgement this patch destroys. It therefore PRINTS the row
count and the status breakdown before deleting, so the loss appears in the migrate log rather
than happening silently, and Frappe Cloud's own backups remain the way to recover it.
Idempotent: doctypes and reports that are already gone are skipped.
"""
import frappe


def _drop_doctype(name):
    if not frappe.db.exists("DocType", name):
        print("=== v17: %s already absent ===" % name)
        return
    try:
        n = frappe.db.count(name)
    except Exception:
        n = "?"
    print("=== v17: deleting DocType %s (%s rows) ===" % (name, n))
    # delete_doc on the DocType drops its table; the rows go with it.
    frappe.delete_doc("DocType", name, force=True, ignore_missing=True, delete_permanently=True)


def execute():
    # 1. Say out loud what is about to be lost.
    if frappe.db.exists("DocType", "Evaluation"):
        try:
            rows = frappe.get_all("Evaluation", fields=["status"], limit_page_length=0)
            tally = {}
            for r in rows:
                tally[r.status or "(none)"] = tally.get(r.status or "(none)", 0) + 1
            print("=== v17: %d Evaluation rows will be deleted %s ===" % (len(rows), tally))
        except Exception:
            pass

    # 2. The facilitator report and its workspace shortcuts.
    if frappe.db.exists("Report", "Pending Evaluations"):
        frappe.delete_doc("Report", "Pending Evaluations", force=True, ignore_missing=True)
        print("=== v17: removed the Pending Evaluations report ===")
    try:
        ws = frappe.get_doc("Workspace", "Hikmat")
        before = len(ws.get("shortcuts") or [])
        ws.shortcuts = [s for s in (ws.get("shortcuts") or [])
                        if s.link_to not in ("Hikmat Milestone", "Pending Evaluations")]
        if len(ws.shortcuts) != before:
            ws.save(ignore_permissions=True)
            print("=== v17: removed %d Desk shortcut(s) ===" % (before - len(ws.shortcuts)))
    except frappe.DoesNotExistError:
        pass
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v17 workspace cleanup")

    # 3. Evaluation first — it Links to Hikmat Milestone, so dropping the target first would
    #    leave a dangling link field behind.
    _drop_doctype("Evaluation")
    _drop_doctype("Hikmat Milestone")

    # 4. Bust the cached settings payload, or clients keep receiving belt thresholds for up to
    #    an hour after the deploy and go on drawing a feature the server no longer has.
    try:
        from hikmat.api import SETTINGS_CACHE_KEY, COURSES_CACHE_KEY, STRUCTURE_CACHE_KEY
        for k in (SETTINGS_CACHE_KEY, COURSES_CACHE_KEY, STRUCTURE_CACHE_KEY):
            frappe.cache().delete_value(k)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "v17 settings cache bust")
    frappe.clear_cache()
    frappe.db.commit()
    print("=== v17: milestone belt gates removed ===")
