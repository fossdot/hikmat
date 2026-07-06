# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# Two students draw independent random n-subsets from a bank of M questions, so a
# question on one paper appears on the other with probability n/M — the expected
# overlap as a fraction of a paper is n/M. Keeping M >= 10*n holds that at <= ~10%,
# and gives each student floor(M/n) fully-fresh papers before any question repeats.
OVERLAP_FACTOR = 10


class ModuleTest(Document):
	def validate(self):
		n = int(self.questions_per_paper or 0)
		if n < 1:
			frappe.throw("Questions per paper must be at least 1.")
		if not (1 <= int(self.pass_pct or 0) <= 100):
			frappe.throw("Pass % must be between 1 and 100.")
		if int(self.time_limit_secs or 0) < 60:
			frappe.throw("Time limit must be at least 60 seconds.")
		bank = self.questions or []
		if len(bank) < n:
			frappe.throw(
				f"The question bank has {len(bank)} questions but each paper needs {n}. "
				"Add more questions or lower Questions per paper.")
		for q in bank:
			choices = [c.strip() for c in (q.choices or "").splitlines() if c.strip()]
			if len(choices) < 2:
				frappe.throw(f"Question {q.idx}: needs at least 2 choices (one per line).")
			if (q.answer or "").strip() not in choices:
				frappe.throw(f"Question {q.idx}: the answer must be exactly one of the choices.")
		if len(bank) < OVERLAP_FACTOR * n:
			frappe.msgprint(
				f"The bank has {len(bank)} questions for {n}-question papers. "
				f"With fewer than {OVERLAP_FACTOR * n}, two students' papers may share more "
				f"than 10% of questions, and a student gets only {len(bank) // n} fresh "
				"papers before questions repeat. Consider adding more questions.",
				title="Small question bank", indicator="orange")
