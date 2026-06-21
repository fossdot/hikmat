"""Install hooks — make a fresh Hikmat site come up ready to play.

On install, Frappe creates the DocTypes from the app's committed JSON (the schema).
This seeds the *data* (grade bands, subjects, tracks/lessons/quiz, settings) and sets
up the teacher backoffice, so a brand-new deployment (e.g. on Frappe Cloud) serves the
game with full content at /play immediately. It does NOT create demo students/attempts —
those are local-testing only (run hikmat.setup_data.demo_students manually if wanted).
"""
import frappe

from hikmat import setup_data as m


def after_install():
    # content: bands + subjects + the 14 tracks / 74 lessons / quiz + Hikmat Settings
    m.seed_content()

    # teacher backoffice (dashboard, charts, workspace) — best-effort, never block install
    try:
        m.setup_analytics()
    except Exception:
        frappe.log_error(title="Hikmat after_install: analytics/workspace setup failed")

    frappe.db.commit()
    print("=== Hikmat after_install: seeded content + backoffice ===")
