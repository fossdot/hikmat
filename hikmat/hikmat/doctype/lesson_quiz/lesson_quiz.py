# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class LessonQuiz(Document):
	def validate(self):
		# Catch the #1 authoring slip: an answer that doesn't exactly match any choice
		# (often a stray trailing space) makes the quiz permanently unwinnable in the game.
		choices = [c.strip() for c in (self.choices or "").split("\n") if c.strip()]
		answer = (self.answer or "").strip()
		if not choices:
			frappe.throw(_("Add at least one choice (one per line)."))
		if answer not in choices:
			frappe.throw(_("Answer must exactly match one of the Choices (check for stray spaces)."))
