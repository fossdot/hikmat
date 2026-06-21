# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class LessonCode(Document):
	def validate(self):
		# The answer must exactly match one of the choices, else the blank is unfillable.
		choices = [c.strip() for c in (self.choices or "").split("\n") if c.strip()]
		answer = (self.answer or "").strip()
		if not choices:
			frappe.throw(_("Add at least one choice (one per line)."))
		if answer not in choices:
			frappe.throw(_("Answer must exactly match one of the Choices (check for stray spaces)."))
		# The code must contain exactly one blank marker.
		if (self.code or "").count("___") != 1:
			frappe.throw(_("Code must contain exactly one blank marker (___)."))
