# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Bring an existing (migrated) site up to the Boli corpus pipeline.

Phase 1 of Boli adds new doctypes (Boli Transcription/Verification/Speaker/Village/XP
Ledger) and new fields on Dialect Capture; the DocType JSON is applied by the normal
migrate. This one-shot, idempotent patch seeds the runtime data they need and folds the
already-shipped self-transcribed captures into the new four-stage pipeline:

  1. seed a starter Boli Village picklist (PAs add the rest in Desk),
  2. seed Hikmat Settings.boli with default tunables + marker set + style guide
     (only if unset — never clobber a PA's edits),
  3. migrate legacy Dialect Capture rows: each self-transcription becomes a
     Boli Transcription (v1) authored by the recorder, with a self Boli Speaker, and the
     clip enters verification so peers can confirm it into the corpus.

Re-runnable: villages/speakers/transcriptions are get-or-create, settings is set-if-empty,
and a clip already carrying a transcription is skipped.
"""
import json

import frappe

from hikmat.api import _BOLI_DEFAULTS, _resolve_boli_speaker

# A small starter set of West Champaran blocks/villages. The picklist exists so speaker
# provenance is never free-typed PII; PAs extend it in Desk.
_STARTER_VILLAGES = [
    "Bettiah", "Bagaha", "Narkatiaganj", "Ramnagar", "Lauriya",
    "Chanpatia", "Gaunaha", "Mainatanr", "Sikta", "Majhaulia", "Bhitaha", "Thakraha",
]

_STYLE_GUIDE = """# Boli transcription — quick guide (जल्दी गाइड)

- जो बोला गया है वही लिखें — शब्द बदलें नहीं, अपनी बोली में लिखें।
- संख्याएँ शब्दों में लिखें ("do", not "2")।
- कोड-मिक्स को मार्क करें: [hindi]…[/hindi], [english]…[/english]।
- साफ़ न सुनाई दे तो [unclear], हँसी [laugh], शोर [noise]।
"""


def _seed_villages():
    for v in _STARTER_VILLAGES:
        if not frappe.db.exists("Boli Village", v):
            frappe.get_doc({"doctype": "Boli Village", "village_name": v,
                            "district": "West Champaran", "active": 1}).insert(ignore_permissions=True)


def _seed_boli_settings():
    if frappe.db.get_single_value("Hikmat Settings", "boli"):
        return                                    # already configured — leave PA edits alone
    cfg = dict(_BOLI_DEFAULTS)
    cfg["markers"] = ["[hindi]", "[english]", "[unclear]", "[laugh]", "[noise]"]
    cfg["style_guide"] = _STYLE_GUIDE
    frappe.db.set_single_value("Hikmat Settings", "boli", json.dumps(cfg, ensure_ascii=False))


def _migrate_legacy_captures():
    rows = frappe.get_all("Dialect Capture",
                          fields=["name", "student", "dialect_transcription", "captured_on",
                                  "consent_status", "prompt_type"])
    for r in rows:
        if frappe.db.exists("Boli Transcription", {"clip": r.name}):
            continue                              # already migrated
        updates = {}
        if not r.get("prompt_type"):
            updates["prompt_type"] = "in_class"
        if not r.get("consent_status"):
            updates["consent_status"] = "self_consented"
        if r.get("student"):
            updates["speaker"] = _resolve_boli_speaker(r.student, relation="self",
                                                       consent_status="self_consented")
            updates["speaker_relation"] = "self"
        text = (r.get("dialect_transcription") or "").strip()
        if text and r.get("student"):
            frappe.get_doc({
                "doctype": "Boli Transcription", "clip": r.name, "author": r.student,
                "text": text[:2000], "version": 1, "is_final": 0,
                "submitted_at": r.get("captured_on") or frappe.utils.now(),
            }).insert(ignore_permissions=True)
            updates["status"] = "in_verification"   # peers can now confirm it into the corpus
        else:
            updates["status"] = "recorded"          # no usable transcription → needs one
        if updates:
            frappe.db.set_value("Dialect Capture", r.name, updates, update_modified=False)


def execute():
    _seed_villages()
    _seed_boli_settings()
    _migrate_legacy_captures()
    frappe.db.commit()
