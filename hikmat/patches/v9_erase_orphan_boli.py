# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Erase the Boli voice data of children whose Student record is already gone.

`delete_student` now walks the whole Boli trail (api._erase_boli_data), but that fix is
FORWARD-ONLY: it cannot help a girl who was deleted BEFORE it existed. Every site that
ran an older build — production included — can therefore still hold her recordings,
transcriptions, XP rows and the real-name→voice bridge (Boli Speaker), with `student`
pointing at a Student row that no longer exists. Frappe's Link fields carry no FK
constraint, so nothing ever noticed. The audio is still on disk and, worse, still LIVE:
this repo's dev site held `Dialect Capture fm3a72089h` (`student='fm38hds9fm'`, deleted)
in status `recorded`, so as soon as its lease expired `get_boli_queue` would hand another
child a deleted girl's voice to transcribe. A right-to-erasure request that was honoured
in the UI was never honoured in the data.

What this patch touches (Boli only — nothing else):
  1. Collects every distinct Student id referenced by a Boli row that no longer resolves
     to a Student, across Dialect Capture.student/operator/claimed_by, Boli
     Transcription.author, Boli Verification.verifier, Boli XP Ledger.student and Boli
     Speaker.student.
  2. Erases each one via `api._erase_boli_data`, the SAME helper the runtime erasure
     path uses. Deliberately NOT reimplemented here: a second copy of the delete order
     would drift from the real one, and the next table added to the pipeline would be
     forgotten in exactly one of the two places. That helper also clears (rather than
     deletes) clips where the vanished girl was only `operator` or `claimed_by` — those
     are ANOTHER child's recordings, so the row survives and the stale transcription
     lease is released back into the queue.
  3. Clears `Dialect Capture.speaker` links left dangling by step 2 (force-deleting a
     Boli Speaker cannot cascade), so no clip points at a speaker row that is gone.
  4. Unlinks those clips' audio with `api._erase_capture_bytes` — AFTER the row deletions
     are committed, the ordering the runtime path uses. Deleting rows alone does not delete
     a child's voice: Frappe dedups uploads on content_hash, so it unlinks a path only once
     no File row shares it, and the bytes outlive the row.
  5. Runs `api._purge_orphan_capture_files`, the maintenance sweep, to reclaim File rows
     whose Dialect Capture parent was already gone before this patch ran — state that step
     4 cannot see, because it only knows the clips it just erased.

Data whose owner is still alive is never touched: rows are selected purely by "the
Student this row names does not exist".

Idempotent / re-runnable: after a successful pass there are no unresolvable references
left, so a second run finds nothing and the file purge is a no-op. Safe on a site whose
Boli doctypes are absent (a pre-Boli install migrating forward) — every lookup is guarded
by an existence check. A per-student failure is logged and skipped rather than aborting
`migrate`, so one bad row cannot block a deploy; the summary printed at the end names
anything left behind so it is not silently lost.
"""
import frappe

# (doctype, fieldname) pairs that point at Student. Hardcoded constants — they are
# interpolated into SQL below and must never come from anywhere else.
_BOLI_STUDENT_REFS = (
    ("Dialect Capture", "student"),      # the recorder — her own voice
    ("Dialect Capture", "operator"),     # she held the phone for someone else
    ("Dialect Capture", "claimed_by"),   # an open transcription lease
    ("Boli Transcription", "author"),
    ("Boli Verification", "verifier"),
    ("Boli XP Ledger", "student"),
    ("Boli Speaker", "student"),         # the real-name → voice bridge
)


def _dangling_students():
    """Student ids named by a Boli row but missing from `tabStudent`, in a stable order.

    A LEFT JOIN, not `frappe.db.exists` per row: on a grown corpus the per-row form is an
    N+1 over a table designed to grow without bound."""
    found = []
    for doctype, fieldname in _BOLI_STUDENT_REFS:
        if not frappe.db.exists("DocType", doctype):
            continue                      # doctype predates this site's Boli build
        rows = frappe.db.sql("""
            select distinct t.`{field}` from `tab{doctype}` t
            left join `tabStudent` s on s.name = t.`{field}`
            where t.`{field}` is not null and t.`{field}` != '' and s.name is null
        """.format(field=fieldname, doctype=doctype))
        for (ref,) in rows:
            if ref not in found:
                found.append(ref)
    return found


def _clear_dangling_speaker_links():
    """Erasing a Boli Speaker uses force=1 (Frappe would otherwise refuse the delete),
    which leaves any clip that referenced it pointing at nothing. Null those out so the
    Desk form and the curation/export paths never resolve a speaker that is gone."""
    if not (frappe.db.exists("DocType", "Dialect Capture")
            and frappe.db.exists("DocType", "Boli Speaker")):
        return 0
    rows = frappe.db.sql("""
        select dc.name from `tabDialect Capture` dc
        left join `tabBoli Speaker` sp on sp.name = dc.speaker
        where dc.speaker is not null and dc.speaker != '' and sp.name is null""", pluck=True)
    for name in rows:
        frappe.db.set_value("Dialect Capture", name, "speaker", None, update_modified=False)
    return len(rows)


def execute():
    # imported here so a site missing the Boli build still imports the patch module
    from hikmat.api import (_erase_boli_data, _erase_capture_bytes,
                            _purge_orphan_capture_files, _scrub_name_residue)

    gone = _dangling_students()
    erased, failed, bytes_removed = [], [], 0
    for student in gone:
        # exactly delete_student's ordering: all ROW deletions in one transaction, commit,
        # then the bytes. Filesystem deletes cannot be rolled back, so unlinking before the
        # commit is what leaves a clip whose audio 500s (see api._erase_capture_bytes).
        try:
            receipt = []
            paths = _erase_boli_data(student, receipt)
            # her name/id also sits in Frappe's own bookkeeping (delete-feed Comments,
            # Versions, …), which nothing else reaches. `user=` is unavailable here on
            # purpose: with the Student row already gone there is no link left to the
            # synthetic Frappe User of an online learner, so that row is out of this
            # patch's reach and stays a manual step.
            _scrub_name_residue(receipt, student=student)
            frappe.db.commit()
            erased.append(student)        # commit per student: a later failure must not
        except Exception:                 # roll back an erasure already completed
            frappe.db.rollback()
            failed.append(student)
            frappe.log_error("v9_erase_orphan_boli: could not erase " + str(student),
                             frappe.get_traceback())
            continue
        bytes_removed += _erase_capture_bytes(paths) or 0

    cleared = _clear_dangling_speaker_links()
    frappe.db.commit()

    stale_files = swept = 0
    if frappe.db.exists("DocType", "Dialect Capture"):
        # Pass 1 of the sweep is the only thing that reaches File rows whose Dialect Capture
        # parent was deleted by an old build — a real state on migrated sites, and one
        # _erase_capture_bytes cannot see because it works from the clips it just erased.
        # Called with the PRODUCTION age guard (never 0): a migrate runs while girls may be
        # uploading, and _save_dialect_capture writes the audio before its commit, so a
        # zero-age sweep could delete a recording still in flight.
        stale_files, swept = _purge_orphan_capture_files()
        frappe.db.commit()

    print("v9_erase_orphan_boli: erased %d orphaned learner(s) %s, unlinked %d audio file(s), "
          "cleared %d dangling speaker link(s), reclaimed %d stale File row(s) and %d "
          "unclaimed file(s)"
          % (len(erased), erased or "", bytes_removed, cleared, stale_files, swept))
    if failed:
        # loud, but not fatal — a blocked deploy would leave the data exposed anyway
        print("v9_erase_orphan_boli: STILL PRESENT, needs manual erasure: %s "
              "(see Error Log)" % failed)
