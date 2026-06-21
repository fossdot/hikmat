# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Contract + validation tests for the public API. Run with:
    bench --site <site> run-tests --app hikmat
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from hikmat import api


class TestHikmatApi(FrappeTestCase):
	def test_signup_rejects_short_name(self):
		self.assertEqual(api.signup_student(name="A").get("error"), "bad_name")

	def test_signup_rejects_short_pin(self):
		# PIN must be 4–8 digits
		self.assertEqual(api.signup_student(name="Test Kid", pin="12").get("error"), "bad_pin")

	def test_login_by_name_is_non_enumerating(self):
		# an unknown name returns the same generic error as a wrong PIN (no "does this name exist?")
		self.assertEqual(api.login_by_name(name="Definitely Nobody Xyz", pin="9999").get("error"), "bad_login")

	def test_submit_attempt_rejects_unknown_student(self):
		self.assertEqual(api.submit_attempt(student="nonexistent-xyz", track="t").get("error"), "unknown_student")

	def test_get_courses_shape(self):
		courses = api.get_courses()
		self.assertIsInstance(courses, list)
		for t in courses:
			for key in ("key", "title", "lessons", "band", "subject"):
				self.assertIn(key, t)
			self.assertIsInstance(t["lessons"], list)

	def test_get_structure_shape(self):
		st = api.get_structure()
		self.assertIn("bands", st)
		self.assertIn("subjects", st)
