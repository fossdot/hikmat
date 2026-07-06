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

	def test_signup_requires_a_pin(self):
		# Phase-0 hardening: a PIN is now mandatory (no more PIN-less, open profiles)
		self.assertEqual(api.signup_student(name="Pinless Kid").get("error"), "bad_pin")

	def test_pin_ok_is_fail_closed(self):
		from werkzeug.security import generate_password_hash
		h = generate_password_hash("1234", method="pbkdf2:sha256")
		self.assertFalse(api._pin_ok("", "1234"))    # no stored PIN → cannot authenticate (was: True)
		self.assertFalse(api._pin_ok(h, ""))         # no PIN supplied
		self.assertFalse(api._pin_ok(h, "9999"))     # wrong PIN
		self.assertTrue(api._pin_ok(h, "1234"))      # correct PIN

	def test_token_valid_window(self):
		self.assertFalse(api._token_valid(None))
		self.assertTrue(api._token_valid(frappe.utils.now()))
		stale = frappe.utils.add_to_date(frappe.utils.now(), days=-(api._TOKEN_TTL_DAYS + 1))
		self.assertFalse(api._token_valid(stale))

	def test_token_ok_fail_closed_and_expiry(self):
		stu = frappe.get_doc({"doctype": "Student", "student_name": "Token Test Girl",
		                      "active": 1, "gender": "Other"}).insert(ignore_permissions=True)
		self.assertFalse(api._token_ok(stu.name, "whatever"))     # no token yet → rejected (was: True)
		tok = api._token_for(stu.name)                            # mint
		self.assertTrue(api._token_ok(stu.name, tok))
		self.assertFalse(api._token_ok(stu.name, "wrong-token"))
		stale = frappe.utils.add_to_date(frappe.utils.now(), days=-(api._TOKEN_TTL_DAYS + 1))
		frappe.db.set_value("Student", stu.name, "token_issued_on", stale, update_modified=False)
		self.assertFalse(api._token_ok(stu.name, tok))            # right value but expired → rejected

	def test_login_rejects_pinless_profile(self):
		stu = frappe.get_doc({"doctype": "Student", "student_name": "No Pin Girl",
		                      "active": 1, "gender": "Other"}).insert(ignore_permissions=True)
		self.assertEqual(api.login_student(student=stu.name).get("error"), "no_pin")

	def test_authorized_token_path(self):
		stu = frappe.get_doc({"doctype": "Student", "student_name": "Auth Path Girl",
		                      "active": 1, "gender": "Other"}).insert(ignore_permissions=True)
		tok = api._token_for(stu.name)
		self.assertTrue(api._authorized(stu.name, tok))          # matching campus token
		self.assertFalse(api._authorized(stu.name, "wrong"))     # bad token, no linked session
		self.assertFalse(api._authorized("does-not-exist", None))

	def test_signup_online_rejects_bad_username(self):
		self.assertEqual(api.signup_online(username="a", pin="1234", invite_code="x").get("error"),
		                 "bad_username")

	def test_signup_online_rejects_bad_pin(self):
		self.assertEqual(api.signup_online(username="asha01", pin="12", invite_code="x").get("error"),
		                 "bad_pin")

	def test_signup_online_rejects_bad_invite(self):
		# valid username + pin, but the invite code matches no cohort → generic bad_invite
		self.assertEqual(
			api.signup_online(username="asha01", pin="1234", invite_code="nope-nope").get("error"),
			"bad_invite")

	def test_get_my_student_resolves_session(self):
		email = "sess_test@" + api._ONLINE_EMAIL_DOMAIN
		# a later test's mid-suite commit can bake this row in — clean before AND after
		frappe.db.delete("Student", {"user": email})
		self.addCleanup(lambda: (frappe.db.delete("Student", {"user": email}), frappe.db.commit()))
		if not frappe.db.exists("User", email):
			u = frappe.get_doc({"doctype": "User", "email": email, "first_name": "Sess Test",
			                    "user_type": "Website User", "enabled": 1, "send_welcome_email": 0})
			u.flags.no_welcome_mail = True
			u.insert(ignore_permissions=True)
		stu = frappe.get_doc({"doctype": "Student", "student_name": "Sess Test Girl", "gender": "Other",
		                      "active": 1, "mode": "Online", "user": email}).insert(ignore_permissions=True)
		frappe.set_user(email)
		try:
			r = api.get_my_student()
			self.assertTrue(r.get("ok"))
			self.assertEqual(r.get("id"), stu.name)
			self.assertTrue(r.get("token"))
		finally:
			frappe.set_user("Administrator")

	def test_get_my_student_guest_is_empty(self):
		frappe.set_user("Guest")
		try:
			self.assertFalse(api.get_my_student().get("ok"))
		finally:
			frappe.set_user("Administrator")

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

	# ---------------- Milestone "belt" gates ----------------
	# _check_milestones commits mid-test (it must — the attempt is already committed in
	# real use), which defeats FrappeTestCase's rollback. So every helper registers an
	# explicit cleanup; nothing test-made may survive into the live DB.
	def _mk_test_milestone(self):
		if not frappe.db.exists("Hikmat Milestone", "belt_test"):
			frappe.get_doc({"doctype": "Hikmat Milestone", "milestone_key": "belt_test",
			                "title": "Test Belt", "threshold_gems": 20, "active": 1,
			                "sort_order": 99}).insert(ignore_permissions=True)
		def _rm():
			frappe.db.delete("Evaluation", {"milestone": "belt_test"})
			frappe.db.delete("Hikmat Milestone", {"name": "belt_test"})
			frappe.db.commit()
			api.clear_content_cache()
		self.addCleanup(_rm)

	def _mk_student(self, name):
		def _rm():
			for s in frappe.get_all("Student", filters={"student_name": name}, pluck="name"):
				frappe.db.delete("Evaluation", {"student": s})
				frappe.db.delete("Lesson Attempt", {"student": s})
				frappe.db.delete("Student", {"name": s})
			frappe.db.commit()
		self.addCleanup(_rm)
		return frappe.get_doc({"doctype": "Student", "student_name": name,
		                       "active": 1, "gender": "Other"}).insert(ignore_permissions=True)

	def _attempt(self, stu, lesson, activity, stars, coins=0):
		return frappe.get_doc({"doctype": "Lesson Attempt", "student": stu.name,
		                       "track": "t1", "lesson": lesson, "activity": activity,
		                       "stars": stars, "coins": coins,
		                       "attempted_on": frappe.utils.now()}).insert(ignore_permissions=True)

	def test_total_gems_accumulates_across_replays(self):
		stu = self._mk_student("Belt Sum Girl")
		self._attempt(stu, "l1", "learn", 2, coins=40)
		self._attempt(stu, "l1", "learn", 3, coins=55)   # replaying the same activity still earns
		self._attempt(stu, "l1", "spell", 1, coins=25)
		self.assertEqual(api._total_gems(stu.name), 120)

	def test_milestone_crossing_creates_pending_evaluation(self):
		stu = self._mk_student("Belt Cross Girl")
		sinfo = frappe._dict(student_name=stu.student_name, cohort=None)
		self._mk_test_milestone()   # a tiny milestone the student has already crossed
		self._attempt(stu, "l1", "learn", 3, coins=30)
		crossed = api._check_milestones(stu.name, sinfo)
		self.assertEqual(crossed, "belt_test")
		ev = frappe.get_doc("Evaluation", {"student": stu.name, "milestone": "belt_test"})
		self.assertEqual(ev.status, "Pending")
		self.assertEqual(ev.gems_at_reach, 30)
		# idempotent: crossing again never makes a second row
		self.assertIsNone(api._check_milestones(stu.name, sinfo))
		self.assertEqual(frappe.db.count("Evaluation",
			{"student": stu.name, "milestone": "belt_test"}), 1)

	def test_get_progress_returns_gates(self):
		stu = self._mk_student("Belt Gate Girl")
		tok = api._token_for(stu.name)
		self._mk_test_milestone()
		frappe.get_doc({"doctype": "Evaluation", "student": stu.name,
		                "milestone": "belt_test", "status": "Passed",
		                "reached_on": frappe.utils.now()}).insert(ignore_permissions=True)
		res = api.get_progress(student=stu.name, token=tok)
		self.assertEqual(res.get("gates", {}).get("belt_test"), "Passed")

	def test_settings_payload_carries_milestones(self):
		s = api._build_settings()
		self.assertIn("milestones", s)
		for m in s["milestones"]:
			for key in ("key", "title", "titleHi", "icon", "threshold"):
				self.assertIn(key, m)

	# ---------------- Learning-event stream (wrong answers) ----------------
	def test_log_event_rejects_unknown_kind(self):
		self.assertEqual(api.log_event(kind="dance_party").get("error"), "bad_kind")

	def test_log_event_anonymous_and_idempotent(self):
		def _rm():
			frappe.db.delete("Learning Event", {"client_id": "t-ev-1"})
			frappe.db.commit()
		self.addCleanup(_rm)
		r1 = api.log_event(kind="wrong_answer", track="t1", lesson="l1", activity="quiz",
		                   question="2+2?", chosen="5", answer="4", client_id="t-ev-1")
		self.assertTrue(r1.get("ok"))
		r2 = api.log_event(kind="wrong_answer", track="t1", lesson="l1", activity="quiz",
		                   question="2+2?", chosen="5", answer="4", client_id="t-ev-1")
		self.assertTrue(r2.get("dedup"))
		self.assertEqual(frappe.db.count("Learning Event", {"client_id": "t-ev-1"}), 1)

	def test_log_event_rejects_wrong_token_for_student(self):
		stu = self._mk_student("Event Auth Girl")
		r = api.log_event(kind="wrong_answer", student=stu.name, token="forged",
		                  question="q", chosen="a", answer="b")
		self.assertEqual(r.get("error"), "auth")

	# ---------------- Learning-event stream (dwell + tool use) ----------------
	def test_log_event_dwell_records_and_caps_duration(self):
		def _rm():
			frappe.db.delete("Learning Event", {"client_id": ("in", ["t-dw-1", "t-dw-2"])})
			frappe.db.commit()
		self.addCleanup(_rm)
		r = api.log_event(kind="dwell", track="t1", lesson="l1", activity="spell",
		                  duration_secs=95, client_id="t-dw-1")
		self.assertTrue(r.get("ok"))
		self.assertEqual(frappe.db.get_value("Learning Event", {"client_id": "t-dw-1"},
		                                     "duration_secs"), 95)
		# a left-open-overnight tab can't poison the time averages: hard 2h cap
		r2 = api.log_event(kind="dwell", track="t1", lesson="l1", activity="spell",
		                   duration_secs=999999, client_id="t-dw-2")
		self.assertTrue(r2.get("ok"))
		self.assertEqual(frappe.db.get_value("Learning Event", {"client_id": "t-dw-2"},
		                                     "duration_secs"), 7200)

	def test_log_event_dwell_requires_duration(self):
		self.assertEqual(api.log_event(kind="dwell", track="t1").get("error"), "bad_duration")

	def test_log_event_tool_use_batches_count(self):
		def _rm():
			frappe.db.delete("Learning Event", {"client_id": "t-tool-1"})
			frappe.db.commit()
		self.addCleanup(_rm)
		r = api.log_event(kind="tool_use", tool="listen_word", track="t1", lesson="l1",
		                  activity="learn", count=7, client_id="t-tool-1")
		self.assertTrue(r.get("ok"))
		row = frappe.db.get_value("Learning Event", {"client_id": "t-tool-1"},
		                          ["tool", "count"], as_dict=True)
		self.assertEqual(row.tool, "listen_word")
		self.assertEqual(row.count, 7)

	def test_log_event_tool_use_requires_tool(self):
		self.assertEqual(api.log_event(kind="tool_use", count=3).get("error"), "bad_tool")

	def test_submit_attempt_stores_capped_duration(self):
		stu = self._mk_student("Dwell Girl")
		tok = api._token_for(stu.name)
		r = api.submit_attempt(student=stu.name, token=tok, track="t1", lesson="l1",
		                       activity="quiz", stars=2, score=4, total=5,
		                       duration_secs=999999, client_id="t-att-dur-1")
		self.assertTrue(r.get("ok"))
		self.assertEqual(frappe.db.get_value("Lesson Attempt", {"client_id": "t-att-dur-1"},
		                                     "duration_secs"), 7200)
