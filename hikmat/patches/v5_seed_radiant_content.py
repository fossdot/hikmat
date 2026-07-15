# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Reseed curriculum content + belt milestones on EXISTING (migrated) sites.

Content and milestone definitions are seeded by `after_install` (hikmat.install),
which runs ONLY on a fresh install: on install Frappe marks patches complete without
executing them, and conversely install hooks never re-run on update. So a site first
installed on an OLD build (prod: June `2c86d60`) and then migrated forward keeps its
stale Track/Lesson set — it never receives content added later (the Radiant Path
curriculum, ~203 lessons across 26 tracks) nor the belt milestones (added 2026-07-05,
after that install).

This one-shot patch brings a migrated site in line with a fresh install's content:
  - seed_content(): wipe + recreate Track/Lesson/Dialogue from data/curriculum.json.
    Student data is UNTOUCHED — Lesson Attempt/Doubt/Event store track/lesson as plain
    strings, and recreated docs keep the same names (Track autonames by track_key,
    Lesson by {track}-{lesson_key}), so those references still resolve.
  - seed_milestones(): idempotent get-or-create of the gem/belt milestones.

Operational defaults (campus, cohorts, invite code, login settings) are intentionally
NOT re-run here — patches v1-v4 already established them on migrated sites, and leaving
them alone guarantees an existing invite code is never disturbed. Idempotent / re-runnable.
"""
import frappe

from hikmat import setup_data


def execute():
    setup_data.seed_content()
    setup_data.seed_milestones()
    frappe.db.commit()
