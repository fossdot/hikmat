# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Remove the Bhojpuri AI ("Boli") corpus feature and erase everything it collected.

The whole pipeline — Record → Transcribe → Verify → Curate, the standalone भोजपुरी AI
tab in the game, the six doctypes behind it and the Dialect Prompt bank — was removed
from the codebase. Deleting the code does NOT delete what it already gathered: Frappe
leaves an app's tables and their `tabDocType` rows in place when the JSON files vanish,
so without this patch a migrated site keeps every recording, every transcription, and
the real-name→voice bridge (Boli Speaker) sitting in the database forever, unreachable
from the UI and therefore unauditable. The most sensitive thing this app ever held was
a child's voice; "the feature is gone" has to mean the audio is gone too.

What it does, in this order (the order matters):

  1. Reports, BEFORE deleting anything, which learners hold Boli XP. Gems 💎 used to be
     lesson coins PLUS corpus XP, and `api._total_gems` no longer adds the second term
     because the ledger is being dropped. Those girls' server-side gem totals therefore
     fall, which can put them back below a milestone belt they had already reached.
     Evaluations already created are untouched — but the NEXT belt gets further away, so
     the affected names are printed for a facilitator to compensate by hand rather than
     the loss happening silently. (Client-side `state.coins` in localStorage is not
     recomputed from the server, so on-device totals do not visibly drop.)
  2. Unlinks the audio: the private File rows attached to Dialect Capture, then the bytes
     on disk that no surviving File row still claims (Frappe dedups uploads on
     content_hash without scoping them to a parent, so two identical clips share one
     path). Rows first, bytes after the commit — a filesystem delete cannot be rolled
     back, so the other order leaves a rolled-back erasure serving audio that is gone.
  3. Drops the rows, then the `tabDocType` rows, then the physical tables. Frappe's
     delete_doc("DocType") clears the metadata but never issues DROP TABLE (see
     frappe/installer.py:remove_app, which has to do it separately) — so a patch that
     only calls delete_doc leaves every recording still readable via raw SQL.
  4. Clears the Desk furniture that pointed at all this: the Boli Adjudication Queue
     report, the "Dialect Captures" workspace shortcut, and the orphaned `boli` value in
     `tabSingles` left behind by removing the field from Hikmat Settings.

Runs post_model_sync, which is safe here precisely because model sync ignores doctypes
whose files are gone — nothing recreates what this deletes.

Idempotent: every step is guarded by an existence check, so a second run finds nothing,
and a fresh install (which never had the doctypes) exits at the first check. A failure in
the audio pass is logged and skipped rather than aborting `migrate` — a blocked deploy
would leave the data in place, which is the outcome this patch exists to prevent.
"""
import os

import frappe

# Hardcoded constants — these are interpolated into DDL below and must never be
# sourced from anywhere else. Child before parent, so a Link never dangles mid-run.
_BOLI_DOCTYPES = ("Boli Verification", "Boli Transcription", "Boli XP Ledger",
                  "Dialect Capture", "Boli Speaker", "Boli Village", "Dialect Prompt")


def _report_gem_loss():
    """Name the learners whose gem total is about to drop, before the ledger goes."""
    if not frappe.db.exists("DocType", "Boli XP Ledger"):
        return
    rows = frappe.db.sql("""
        select x.student, coalesce(s.student_name, '(deleted)') as who, sum(x.points) as pts
        from `tabBoli XP Ledger` x
        left join `tabStudent` s on s.name = x.student
        where x.student is not null and x.student != ''
        group by x.student, s.student_name having sum(x.points) > 0
        order by pts desc""", as_dict=True)
    if not rows:
        print("v12_remove_boli: no learner held corpus XP — no gem totals change")
        return
    print("v12_remove_boli: %d learner(s) lose corpus XP from their gem total "
          "(milestone belts may need clearing by hand):" % len(rows))
    for r in rows:
        print("    %s (%s): -%d gems" % (r.who, r.student, int(r.pts or 0)))


def _private_path(file_url):
    """Absolute path of a private file, or None. Basename only, and only inside the
    private-files directory: `file_url` is a DB column, so a `..` in it must never talk
    this into unlinking something outside that directory."""
    base = (file_url or "").rsplit("/", 1)[-1]
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        return None
    root = os.path.abspath(frappe.utils.get_files_path(is_private=1))
    full = os.path.abspath(os.path.join(root, base))
    return full if os.path.dirname(full) == root else None


def _erase_audio():
    """Delete the clips' File rows, then unlink the bytes no surviving row still claims.

    Deliberately NOT frappe.delete_doc("File", …): File.on_trash resolves its parent, which
    means importing the Dialect Capture controller — a module this commit deleted. Every
    delete then raises, the rows survive, and the patch reports a success it did not
    achieve. (Observed exactly that: 11 File rows left behind, still naming a child's audio
    on disk.) A raw delete needs no controller; the dedup safety File.on_trash provides is
    reimplemented below, which is the only part of it that mattered here.

    Not guarded on the doctype existing: `attached_to_doctype` is a plain string column, so
    these rows stay findable — and stay deletable — after the doctype itself is gone. That
    is what lets a re-run clean up leftovers from an interrupted first pass."""
    files = frappe.get_all("File", filters={"attached_to_doctype": "Dialect Capture"},
                           fields=["name", "file_url"])
    if not files:
        return 0, 0
    names = [f.name for f in files]
    for i in range(0, len(names), 500):
        frappe.db.delete("File", {"name": ["in", names[i:i + 500]]})
    frappe.db.commit()

    # Bytes last, and only once the row deletions are durable: filesystem deletes cannot be
    # rolled back, so unlinking inside the transaction would leave a rolled-back erasure
    # serving clips whose audio is gone. Frappe dedups uploads on content_hash with no
    # parent scoping, so two byte-identical clips share ONE path — unlink only what no
    # surviving File row still points at, or this destroys an unrelated attachment.
    unlinked = 0
    for f in files:
        path = _private_path(f.file_url)
        if not path or not os.path.exists(path):
            continue
        if frappe.db.exists("File", {"file_url": f.file_url}):
            continue                     # another row still claims these bytes
        try:
            os.remove(path)
            unlinked += 1
        except OSError:
            frappe.log_error("v12_remove_boli: could not unlink " + str(path),
                             frappe.get_traceback())
    return len(names), unlinked


def _drop_desk_furniture():
    """The report, its shortcut, and the settings value that now has no field."""
    if frappe.db.exists("Report", "Boli Adjudication Queue"):
        frappe.delete_doc("Report", "Boli Adjudication Queue", force=1,
                          ignore_permissions=True, delete_permanently=True)
    for table, filters in (("Workspace Shortcut", {"link_to": "Dialect Capture"}),
                           ("Workspace Shortcut", {"link_to": "Boli Adjudication Queue"}),
                           ("Workspace Link", {"link_to": "Dialect Capture"}),
                           ("Workspace Link", {"link_to": "Boli Adjudication Queue"}),
                           ("Singles", {"doctype": "Hikmat Settings", "field": "boli"})):
        try:
            frappe.db.delete(table, filters)
        except Exception:
            frappe.log_error("v12_remove_boli: could not clear " + table,
                             frappe.get_traceback())


def execute():
    present = [dt for dt in _BOLI_DOCTYPES if frappe.db.exists("DocType", dt)]
    # Not short-circuited on `present` being empty: an interrupted first pass can leave
    # File rows and Desk furniture behind after the doctypes are already gone, and those
    # are exactly what a re-run has to finish. A site that never had Boli finds nothing
    # in either, so this stays a no-op there.
    _report_gem_loss()
    files_deleted, bytes_unlinked = _erase_audio()

    rows = {}
    for dt in present:
        table = "tab" + dt
        rows[dt] = frappe.db.sql("select count(*) from `%s`" % table)[0][0]
        frappe.delete_doc("DocType", dt, force=1, ignore_missing=True,
                          ignore_permissions=True)
        # delete_doc clears the metadata but leaves the table (and every row in it)
        frappe.db.sql_ddl("drop table if exists `%s`" % table)

    _drop_desk_furniture()
    frappe.db.commit()
    frappe.clear_cache()

    print("v12_remove_boli: removed %d doctype(s) [%s], %d audio File row(s) and %d "
          "audio file(s) on disk" % (len(present), ", ".join(present) or "none",
                                     files_deleted, bytes_unlinked))
    if rows:
        print("v12_remove_boli: rows erased — " + ", ".join(
            "%s=%d" % (dt, n) for dt, n in rows.items()))
