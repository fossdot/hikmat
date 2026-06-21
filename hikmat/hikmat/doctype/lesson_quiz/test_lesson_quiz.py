# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestLessonQuiz(FrappeTestCase):
	def _doc(self, choices, answer):
		return frappe.get_doc({"doctype": "Lesson Quiz", "question": "Q?", "choices": choices, "answer": answer})

	def test_answer_must_match_a_choice(self):
		with self.assertRaises(frappe.ValidationError):
			self._doc("apple\nbanana", "cherry").validate()

	def test_trimmed_answer_passes(self):
		# stray spaces must not make a valid item unwinnable
		self._doc("apple\nbanana", " banana ").validate()

	def test_requires_choices(self):
		with self.assertRaises(frappe.ValidationError):
			self._doc("", "apple").validate()
