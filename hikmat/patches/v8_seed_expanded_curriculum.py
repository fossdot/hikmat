# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Reseed curriculum content on EXISTING (migrated) sites — curriculum expansion.

Content is seeded by `after_install` only on a fresh install, and the one-shot
v5 patch brought migrated sites up to the Radiant Path curriculum (~203 lessons).
This build expands the curriculum again: +4 lessons in every grade-band track
(classes 1-4 / 5-8 / 9-10) and +4 in each of the newer Life Skills worlds
(Health & Clinic, Money & Bank, Travel & Directions) — ~283 lessons total.

Same contract as v5: seed_content() wipes + recreates Track/Lesson/Dialogue
from data/curriculum.json. Student data is UNTOUCHED — attempts,
doubts and events reference track/lesson by stable string keys, and recreated
docs keep the same names (Track autonames by track_key, Lesson by
{track}-{lesson_key}); existing lesson keys are never renamed, new lessons only
append. Idempotent / re-runnable.
"""
import frappe

from hikmat import setup_data


def execute():
    setup_data.seed_content()
    frappe.db.commit()
