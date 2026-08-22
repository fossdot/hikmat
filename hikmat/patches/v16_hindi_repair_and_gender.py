# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Reseed curriculum content — the Hindi repair pass, and gender-marked Hindi.

WHY: three faults were reported from the phone on 2026-08-22 and all three live in the
CONTENT, not the code, so no app update can fix them on its own.

1. UNANSWERABLE HINDI. 18 quiz items had an EMPTY `qHi`, and 54 more dropped the very
   fragment the question hinges on. "Pick the noun: 'The dog runs fast.'" was glossed as
   just "संज्ञा चुनो" — a girl reading Hindi saw "choose the noun" with no sentence to
   choose it from, and three English words below. She could only guess. Every such row now
   carries the English fragment verbatim, in Latin script, inside quotes.

2. ROMANISED HINDI. The `school` track's teaching hints were written in Latin-script Hindi
   ("He, she ya it ke saath has lagta hai"), which a Devanagari reader cannot read and which
   the Hindi TTS voice cannot pronounce at all — so the audio-first learner this app is built
   for got nothing from them. Now Devanagari, with the grammar terms glossed
   (संज्ञा (noun), क्रिया (verb)).

3. HINDI THAT MISGENDERS HALF THE ROSTER. Hindi verbs and adjectives agree with their
   subject, and every learner-voiced line here was written feminine: a boy tapping the reply
   "I will wash my hands" said "मैं हाथ धो लूँगी" out loud. Learner-voiced and
   learner-addressed strings now carry a {feminine|masculine} marker which the game resolves
   from Student.gender (gform() in index.html). Agreement with an OBJECT ("रेलगाड़ी आ गई है")
   or a third person in a story is NOT the child's gender and was deliberately left alone.

640 field-level fixes across 26 tracks, each one adversarially reviewed before it landed.

Same contract as v5/v8: seed_content() wipes and recreates Track/Lesson/Dialogue from
data/curriculum.json. Student data is UNTOUCHED — attempts, doubts and events reference
track/lesson by stable string keys and the recreated docs keep the same names (Track
autonames by track_key, Lesson by {track}-{lesson_key}). No lesson key is renamed here;
only Hindi text changes. Idempotent / re-runnable.
"""
import frappe

from hikmat import setup_data


def execute():
    setup_data.seed_content()
    frappe.db.commit()
