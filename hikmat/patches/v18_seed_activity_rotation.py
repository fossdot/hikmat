# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Reseed curriculum content on EXISTING (migrated) sites — activity rotation, first slice.

This build starts varying WHICH activities each lesson carries (the rotation model): three
new recognition games — Match the Pairs, Odd One Out, Sort the Baskets — plus a per-lesson
`skip` list, all carried in the Lesson's new extras_json field. The Bazaar track's first
three lessons are the pilot: each now runs a different six-step ladder instead of the same
one three times over.

Same contract as v5/v8: seed_content() wipes + recreates Track/Lesson/Dialogue from
data/curriculum.json. Student data is UNTOUCHED — attempts, doubts and events reference
track/lesson by stable string keys, and recreated docs keep the same names. Idempotent /
re-runnable.
"""
import frappe

from hikmat import setup_data


def execute():
    setup_data.seed_content()
    frappe.db.commit()
