# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Boli Adjudication Queue — the clips a Program Associate must act on, oldest first:

  - Escalation   : status=escalated (a verifier escalated, or a transcription was
                   rejected past the rework cap) → PA picks/edits the final transcription.
  - PII hold     : status=pii_hold (curation flagged possible PII) → clear or mark
                   internal_only; blocked from export until resolved.
  - Consent miss : consent_status is missing/blank on a clip that isn't rejected →
                   confirm consent or reject the clip.

This is the Phase-1 PA surface; each row links to the Dialect Capture doc where the
facilitator adjudicates in Desk. Richer corpus-health analytics are a later phase.
"""
import frappe
from frappe import _

# Student free text is rendered by the Desk grid, which assigns cell HTML directly
# ("Data" columns are NOT escaped by frappe's formatter), and the same rows are what
# the CSV/XLSX export writes. Both hazards, and the reason one guard cannot serve
# both destinations, are documented once in hikmat.report_utils — this report used to
# carry its own copy, which is exactly how the export ended up full of &quot;.
from hikmat.report_utils import safe_cell

# The PA's job in this queue is to PICK OR EDIT the final transcription, so she has to
# see all of it: multi-sentence Boli prompts routinely run past 150 Devanagari characters,
# and the old 120-char cut silently dropped the end of a sentence with no ellipsis. Room
# for a long answer, with the column widened to match; anything longer is cut with a
# VISIBLE ellipsis, and only in the grid — safe_cell never truncates the export, which is
# the corpus (see hikmat.report_utils.safe_cell).
_TRANSCRIPTION_GRID_MAX = 600


def execute(filters=None):
    columns = [
        {"label": _("Queue"), "fieldname": "queue", "fieldtype": "Data", "width": 130},
        {"label": _("Clip"), "fieldname": "clip", "fieldtype": "Link", "options": "Dialect Capture", "width": 180},
        {"label": _("Student"), "fieldname": "student_name", "fieldtype": "Data", "width": 140},
        {"label": _("Prompt (hi)"), "fieldname": "prompt_text_hi", "fieldtype": "Data", "width": 220},
        {"label": _("Latest transcription"), "fieldname": "transcription", "fieldtype": "Data", "width": 420},
        {"label": _("Rework"), "fieldname": "rework_rounds", "fieldtype": "Int", "width": 70},
        {"label": _("Captured"), "fieldname": "captured_on", "fieldtype": "Datetime", "width": 150},
    ]

    fields = ["name", "student_name", "prompt_text_hi", "rework_rounds", "captured_on", "status"]
    flagged = frappe.get_all("Dialect Capture", filters=[["status", "in", ["escalated", "pii_hold"]]],
                             fields=fields, order_by="captured_on asc")
    consent = frappe.get_all("Dialect Capture",
                             filters=[["consent_status", "in", ["", "missing"]],
                                      ["status", "not in", ["rejected"]]],
                             fields=fields, order_by="captured_on asc")

    rows, seen = [], set()

    def add(c, queue):
        if c.name in seen:
            return
        seen.add(c.name)
        tr = frappe.db.get_value("Boli Transcription", {"clip": c.name}, "text",
                                 order_by="version desc") or ""
        rows.append({"queue": queue, "clip": c.name,
                     "student_name": safe_cell(c.student_name),
                     "prompt_text_hi": safe_cell(c.prompt_text_hi),
                     "transcription": safe_cell(tr, _TRANSCRIPTION_GRID_MAX),
                     "rework_rounds": c.rework_rounds, "captured_on": c.captured_on})

    for c in flagged:
        add(c, _("PII hold") if c.status == "pii_hold" else _("Escalation"))
    for c in consent:
        add(c, _("Consent missing"))
    return columns, rows
