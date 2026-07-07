# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class LessonRead(Document):
	def validate(self):
		# Same authoring guard as Lesson Quiz: an answer that doesn't exactly match a
		# choice makes the comprehension question permanently unwinnable in the game.
		choices = [c.strip() for c in (self.choices or "").split("\n") if c.strip()]
		answer = (self.answer or "").strip()
		if not (self.passage or "").strip():
			frappe.throw(_("Add the reading passage."))
		if not (self.question or "").strip():
			frappe.throw(_("Add the question."))
		if not choices:
			frappe.throw(_("Add at least one choice (one per line)."))
		if answer not in choices:
			frappe.throw(_("Answer must exactly match one of the Choices (check for stray spaces)."))
