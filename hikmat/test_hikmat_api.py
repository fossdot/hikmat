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


class TestModuleTests(FrappeTestCase):
	"""Module-end tests: bank validation, curriculum export, submit_test hardening,
	get_progress tests/testSeen. Mirrors TestHikmatApi's explicit-cleanup style."""

	def _mk_student(self, name):
		def _rm():
			for s in frappe.get_all("Student", filters={"student_name": name}, pluck="name"):
				frappe.db.delete("Test Attempt", {"student": s})
				frappe.db.delete("Student", {"name": s})
			frappe.db.commit()
		self.addCleanup(_rm)
		return frappe.get_doc({"doctype": "Student", "student_name": name,
		                       "active": 1, "gender": "Other"}).insert(ignore_permissions=True)

	def _mk_track(self, key="mt-track"):
		def _rm():
			frappe.db.delete("Module Test", {"track": key})
			frappe.db.delete("Track", {"name": key})
			frappe.db.commit()
			api.clear_content_cache()
		self.addCleanup(_rm)
		if frappe.db.exists("Track", key):
			frappe.delete_doc("Track", key, force=1, ignore_permissions=True)
		return frappe.get_doc({"doctype": "Track", "track_key": key, "title": "MT Track",
		                       "published": 1}).insert(ignore_permissions=True)

	def _q(self, i):
		return {"question": f"Q{i}?", "choices": "a\nb\nc", "answer": "a"}

	def _mk_module_test(self, track, n_questions=10, per_paper=5, pass_pct=60):
		mt = frappe.get_doc({"doctype": "Module Test", "track": track.name, "active": 1,
		                     "questions_per_paper": per_paper, "pass_pct": pass_pct,
		                     "time_limit_secs": 600,
		                     "questions": [self._q(i) for i in range(n_questions)]})
		mt.insert(ignore_permissions=True)
		return mt

	def test_module_test_rejects_answer_not_in_choices(self):
		track = self._mk_track("mt-badq")
		mt = frappe.get_doc({"doctype": "Module Test", "track": track.name,
		                     "questions_per_paper": 1, "pass_pct": 60, "time_limit_secs": 600,
		                     "questions": [{"question": "Q?", "choices": "a\nb", "answer": "zzz"}]})
		self.assertRaises(frappe.ValidationError, mt.insert)

	def test_module_test_rejects_bank_smaller_than_paper(self):
		track = self._mk_track("mt-small")
		mt = frappe.get_doc({"doctype": "Module Test", "track": track.name,
		                     "questions_per_paper": 5, "pass_pct": 60, "time_limit_secs": 600,
		                     "questions": [self._q(i) for i in range(3)]})
		self.assertRaises(frappe.ValidationError, mt.insert)

	def test_module_test_rejects_bad_config(self):
		track = self._mk_track("mt-cfg")
		base = {"doctype": "Module Test", "track": track.name,
		        "questions": [self._q(i) for i in range(3)]}
		for bad in ({"questions_per_paper": 0, "pass_pct": 60, "time_limit_secs": 600},
		            {"questions_per_paper": 1, "pass_pct": 0, "time_limit_secs": 600},
		            {"questions_per_paper": 1, "pass_pct": 101, "time_limit_secs": 600},
		            {"questions_per_paper": 1, "pass_pct": 60, "time_limit_secs": 30}):
			mt = frappe.get_doc({**base, **bad})
			self.assertRaises(frappe.ValidationError, mt.insert)

	def test_track_json_exports_bank_without_answkey_leaks(self):
		track = self._mk_track("mt-export")
		self._mk_module_test(track, n_questions=6, per_paper=5)
		api.clear_content_cache()
		t = next(c for c in api._build_courses() if c["key"] == "mt-export")
		self.assertIn("test", t)
		self.assertEqual(t["test"]["questionsPerPaper"], 5)
		self.assertEqual(t["test"]["passPct"], 60)
		self.assertEqual(len(t["test"]["bank"]), 6)
		for q in t["test"]["bank"]:
			for key in ("id", "q", "choices", "answer"):
				self.assertIn(key, q)
			self.assertNotIn("teach", q)      # facilitator notes never ship in a test

	def test_track_json_skips_inactive_test(self):
		track = self._mk_track("mt-inactive")
		mt = self._mk_module_test(track, n_questions=6, per_paper=5)
		frappe.db.set_value("Module Test", mt.name, "active", 0)
		api.clear_content_cache()
		t = next(c for c in api._build_courses() if c["key"] == "mt-inactive")
		self.assertNotIn("test", t)

	def test_submit_test_rejects_unknown_student_and_bad_token(self):
		self.assertEqual(api.submit_test(student="nope-xyz", track="t",
		                                 status="completed").get("error"), "unknown_student")
		stu = self._mk_student("Test Auth Girl")
		r = api.submit_test(student=stu.name, token="forged", track="t", status="completed")
		self.assertEqual(r.get("error"), "auth")

	def test_submit_test_rejects_bad_status(self):
		stu = self._mk_student("Test Status Girl")
		tok = api._token_for(stu.name)
		r = api.submit_test(student=stu.name, token=tok, track="t", status="hacked")
		self.assertEqual(r.get("error"), "bad_status")

	def test_submit_test_idempotent_on_client_id(self):
		stu = self._mk_student("Test Dedup Girl")
		tok = api._token_for(stu.name)
		kw = dict(student=stu.name, token=tok, track="t1", status="completed",
		          score=4, total=5, client_id="t-test-1")
		r1 = api.submit_test(**kw)
		self.assertTrue(r1.get("ok"))
		r2 = api.submit_test(**kw)
		self.assertTrue(r2.get("dedup"))
		self.assertEqual(frappe.db.count("Test Attempt", {"client_id": "t-test-1"}), 1)

	def test_submit_test_exited_forces_zero(self):
		stu = self._mk_student("Test Void Girl")
		tok = api._token_for(stu.name)
		r = api.submit_test(student=stu.name, token=tok, track="t1", status="exited",
		                    exit_reason="hidden", score=9, total=10, client_id="t-test-void")
		self.assertTrue(r.get("ok"))
		self.assertFalse(r.get("passed"))
		row = frappe.db.get_value("Test Attempt", {"client_id": "t-test-void"},
		                          ["score", "pct", "passed", "status", "exit_reason"], as_dict=True)
		self.assertEqual(row.score, 0)
		self.assertEqual(row.pct, 0)
		self.assertEqual(row.passed, 0)
		self.assertEqual(row.status, "Exited")
		self.assertEqual(row.exit_reason, "hidden")

	def test_submit_test_pass_computed_server_side(self):
		track = self._mk_track("mt-pass")
		self._mk_module_test(track, n_questions=10, per_paper=10, pass_pct=60)
		stu = self._mk_student("Test Pass Girl")
		tok = api._token_for(stu.name)
		r = api.submit_test(student=stu.name, token=tok, track="mt-pass", status="completed",
		                    score=6, total=10, client_id="t-test-p1")
		self.assertTrue(r.get("passed"))
		r = api.submit_test(student=stu.name, token=tok, track="mt-pass", status="completed",
		                    score=5, total=10, client_id="t-test-p2")
		self.assertFalse(r.get("passed"))
		# running out of time is not cheating — answered-so-far still counts
		r = api.submit_test(student=stu.name, token=tok, track="mt-pass", status="timed_out",
		                    score=7, total=10, client_id="t-test-p3")
		self.assertTrue(r.get("passed"))

	def test_submit_test_clamps_and_survives_bad_paper(self):
		stu = self._mk_student("Test Clamp Girl")
		tok = api._token_for(stu.name)
		r = api.submit_test(student=stu.name, token=tok, track="t1", status="completed",
		                    score=99, total=5, paper="not-json[", duration_secs=999999,
		                    client_id="t-test-clamp")
		self.assertTrue(r.get("ok"))
		row = frappe.db.get_value("Test Attempt", {"client_id": "t-test-clamp"},
		                          ["score", "paper", "duration_secs", "attempted_on"], as_dict=True)
		self.assertEqual(row.score, 5)              # clamped to total
		self.assertEqual(row.paper, "[]")           # malformed paper never rejects the write
		self.assertEqual(row.duration_secs, 7200)
		self.assertTrue(row.attempted_on)           # first-exposure ordering depends on this

	def test_get_progress_returns_tests_and_seen_union(self):
		stu = self._mk_student("Test Seen Girl")
		tok = api._token_for(stu.name)
		api.submit_test(student=stu.name, token=tok, track="t1", status="completed",
		                score=3, total=5, paper='["qa","qb"]', client_id="t-seen-1")
		api.submit_test(student=stu.name, token=tok, track="t1", status="exited",
		                exit_reason="blur", score=0, total=5, paper='["qb","qc"]',
		                client_id="t-seen-2")
		res = api.get_progress(student=stu.name, token=tok)
		self.assertIn("t1", res.get("tests", {}))
		self.assertEqual(res["tests"]["t1"]["attempts"], 2)
		self.assertEqual(res["tests"]["t1"]["bestPct"], 60)
		# voided papers still burn: the union is qa, qb, qc in first-exposure order
		self.assertEqual(res.get("testSeen", {}).get("t1"), ["qa", "qb", "qc"])

	def test_log_event_accepts_test_exit(self):
		def _rm():
			frappe.db.delete("Learning Event", {"client_id": "t-texit-1"})
			frappe.db.commit()
		self.addCleanup(_rm)
		r = api.log_event(kind="test_exit", track="t1", activity="test", tool="hidden",
		                  duration_secs=120, count=3, client_id="t-texit-1")
		self.assertTrue(r.get("ok"))
		self.assertEqual(api.log_event(kind="dance_party").get("error"), "bad_kind")

	def test_delete_student_erases_test_attempts(self):
		stu = self._mk_student("Test Erase Girl")
		tok = api._token_for(stu.name)
		api.submit_test(student=stu.name, token=tok, track="t1", status="completed",
		                score=1, total=5, client_id="t-erase-1")
		api.delete_student(stu.name)
		self.assertEqual(frappe.db.count("Test Attempt", {"student": stu.name}), 0)


class TestAttendance(FrappeTestCase):
	"""log_attendance: auth, clamps, dedup, day upsert, thresholds, date window."""

	def _mk_student(self, name):
		def _rm():
			for s in frappe.get_all("Student", filters={"student_name": name}, pluck="name"):
				frappe.db.delete("Attendance Ping", {"student": s})
				frappe.db.delete("Attendance Day", {"student": s})
				frappe.db.delete("Student", {"name": s})
			frappe.db.commit()
		self.addCleanup(_rm)
		return frappe.get_doc({"doctype": "Student", "student_name": name,
		                       "active": 1, "gender": "Other"}).insert(ignore_permissions=True)

	def test_log_attendance_rejects_unknown_student(self):
		r = api.log_attendance(student="nope-xyz", date=frappe.utils.nowdate(), secs=60)
		self.assertEqual(r.get("error"), "unknown_student")

	def test_log_attendance_rejects_bad_token(self):
		stu = self._mk_student("Att Auth Girl")
		r = api.log_attendance(student=stu.name, token="forged",
		                       date=frappe.utils.nowdate(), secs=60)
		self.assertEqual(r.get("error"), "auth")

	def test_log_attendance_clamps_secs(self):
		stu = self._mk_student("Att Clamp Girl")
		tok = api._token_for(stu.name)
		r = api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		                       secs=99999, client_id="t-att-c1")
		self.assertTrue(r.get("ok"))
		self.assertEqual(r.get("secs_today"), 900)   # one ping can never claim >15 min
		r = api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		                       secs=0, client_id="t-att-c2")
		self.assertEqual(r.get("error"), "bad_secs")

	def test_log_attendance_dedups_client_id(self):
		stu = self._mk_student("Att Dedup Girl")
		tok = api._token_for(stu.name)
		kw = dict(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		          secs=300, client_id="t-att-d1")
		self.assertTrue(api.log_attendance(**kw).get("ok"))
		self.assertTrue(api.log_attendance(**kw).get("dedup"))
		day = frappe.db.get_value("Attendance Day",
		                          {"student": stu.name, "date": frappe.utils.nowdate()},
		                          "active_secs")
		self.assertEqual(day, 300)                   # the retry added nothing

	def test_log_attendance_upserts_and_sums(self):
		stu = self._mk_student("Att Sum Girl")
		tok = api._token_for(stu.name)
		api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		                   secs=300, client_id="t-att-s1", device_id="dev-a")
		api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		                   secs=600, client_id="t-att-s2", device_id="dev-b")
		rows = frappe.get_all("Attendance Day",
		                      filters={"student": stu.name, "date": frappe.utils.nowdate()},
		                      fields=["active_secs", "device_count", "first_ping", "last_ping"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].active_secs, 900)
		self.assertEqual(rows[0].device_count, 2)
		self.assertTrue(rows[0].first_ping <= rows[0].last_ping)

	def test_log_attendance_rejects_out_of_range_dates(self):
		stu = self._mk_student("Att Range Girl")
		tok = api._token_for(stu.name)
		too_old = frappe.utils.add_days(frappe.utils.nowdate(), -(api._ATT_PAST_WINDOW_DAYS + 1))
		future = frappe.utils.add_days(frappe.utils.nowdate(), api._ATT_FUTURE_WINDOW_DAYS + 1)
		self.assertEqual(api.log_attendance(student=stu.name, token=tok, date=too_old,
		                                    secs=60).get("error"), "date_out_of_range")
		self.assertEqual(api.log_attendance(student=stu.name, token=tok, date=future,
		                                    secs=60).get("error"), "date_out_of_range")
		ok_old = frappe.utils.add_days(frappe.utils.nowdate(), -api._ATT_PAST_WINDOW_DAYS)
		self.assertTrue(api.log_attendance(student=stu.name, token=tok, date=ok_old,
		                                   secs=60, client_id="t-att-r1").get("ok"))
		self.assertEqual(api.log_attendance(student=stu.name, token=tok, date="garbage",
		                                    secs=60).get("error"), "bad_date")

	def test_present_flips_at_threshold(self):
		stu = self._mk_student("Att Present Girl")
		tok = api._token_for(stu.name)
		# default threshold 150 min = 9000s; 900s/ping → 10 pings to Present
		for i in range(9):
			r = api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
			                       secs=900, client_id=f"t-att-p{i}")
		self.assertFalse(r.get("present"))           # 8100s < 9000s
		r = api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		                       secs=900, client_id="t-att-p9")
		self.assertTrue(r.get("present"))            # 9000s ≥ 9000s

	def test_daily_attendance_report_marks_absent(self):
		from hikmat.hikmat.report.daily_attendance.daily_attendance import execute
		stu = self._mk_student("Att Report Girl")
		today = frappe.utils.nowdate()
		cols, rows, _msg, _chart, summary = execute(
			{"from_date": today, "to_date": today, "student": stu.name})
		mine = [r for r in rows if r["student"] == stu.name]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0]["status"], "Absent")   # no pings → explicit Absent row
		tok = api._token_for(stu.name)
		for i in range(10):
			api.log_attendance(student=stu.name, token=tok, date=today, secs=900,
			                   client_id=f"t-att-rep{i}")
		_c, rows, _m, _ch, _s = execute({"from_date": today, "to_date": today, "student": stu.name})
		mine = [r for r in rows if r["student"] == stu.name]
		self.assertEqual(mine[0]["status"], "Present")
		self.assertEqual(mine[0]["active_minutes"], 150)

	def test_attendance_summary_report_aggregates(self):
		from hikmat.hikmat.report.attendance_summary.attendance_summary import execute
		stu = self._mk_student("Att Summary Girl")
		tok = api._token_for(stu.name)
		today = frappe.utils.nowdate()
		for i in range(10):
			api.log_attendance(student=stu.name, token=tok, date=today, secs=900,
			                   client_id=f"t-att-sum{i}")
		_c, rows, _m, _ch, _s = execute({"from_date": today, "to_date": today})
		mine = [r for r in rows if r["student"] == stu.name]
		self.assertEqual(len(mine), 1)
		self.assertEqual(mine[0]["days_present"], 1)
		self.assertEqual(mine[0]["total_active_hours"], 2.5)

	def test_delete_student_erases_attendance(self):
		stu = self._mk_student("Att Erase Girl")
		tok = api._token_for(stu.name)
		api.log_attendance(student=stu.name, token=tok, date=frappe.utils.nowdate(),
		                   secs=300, client_id="t-att-e1")
		api.delete_student(stu.name)
		self.assertEqual(frappe.db.count("Attendance Ping", {"student": stu.name}), 0)
		self.assertEqual(frappe.db.count("Attendance Day", {"student": stu.name}), 0)


class TestTrackVideo(FrappeTestCase):
	"""Track explainer-video fields: export shape + public-file validation."""

	def _mk_track(self, key, **kw):
		def _rm():
			frappe.db.delete("Track", {"name": key})
			frappe.db.commit()
			api.clear_content_cache()
		self.addCleanup(_rm)
		if frappe.db.exists("Track", key):
			frappe.delete_doc("Track", key, force=1, ignore_permissions=True)
		return frappe.get_doc({"doctype": "Track", "track_key": key, "title": "V Track",
		                       "published": 1, **kw}).insert(ignore_permissions=True)

	def test_get_courses_exposes_video_when_set(self):
		self._mk_track("vid-set", video="/files/expl.mp4", video_title="Watch this",
		               video_title_hi="यह देखो", video_duration_secs=180)
		api.clear_content_cache()
		t = next(c for c in api._build_courses() if c["key"] == "vid-set")
		self.assertEqual(t["videoUrl"], "/files/expl.mp4")
		self.assertEqual(t["videoTitle"], "Watch this")
		self.assertEqual(t["videoTitleHi"], "यह देखो")
		self.assertEqual(t["videoDuration"], 180)

	def test_get_courses_omits_video_keys_when_unset(self):
		self._mk_track("vid-unset")
		api.clear_content_cache()
		t = next(c for c in api._build_courses() if c["key"] == "vid-unset")
		self.assertNotIn("videoUrl", t)
		self.assertNotIn("videoDuration", t)

	def test_track_rejects_private_video(self):
		doc = frappe.get_doc({"doctype": "Track", "track_key": "vid-priv", "title": "P",
		                      "published": 0, "video": "/private/files/x.mp4"})
		self.assertRaises(frappe.ValidationError, doc.insert)


class TestLessonReply(FrappeTestCase):
	"""Reply-to-the-Email activity: curriculum export shape."""

	def test_track_json_exports_reply(self):
		key = "reply-export-t"
		def _rm():
			frappe.db.delete("Lesson", {"track": key})
			frappe.db.delete("Track", {"name": key})
			frappe.db.commit()
			api.clear_content_cache()
		self.addCleanup(_rm)
		if frappe.db.exists("Track", key):
			frappe.delete_doc("Track", key, force=1, ignore_permissions=True)
		track = frappe.get_doc({"doctype": "Track", "track_key": key, "title": "Reply T",
		                        "published": 1}).insert(ignore_permissions=True)
		import json as _json
		frappe.get_doc({"doctype": "Lesson", "track": track.name, "lesson_key": "l1",
		                "title": "L1", "published": 1,
		                "reply": [{"from_name": "Sunita Madam", "subject": "Class time",
		                           "message": "Class starts at 10 tomorrow.",
		                           "message_hi": "कक्षा कल 10 बजे शुरू होगी।",
		                           "spec_json": _json.dumps({"slots": [
		                               {"label": "Greeting", "labelHi": "अभिवादन",
		                                "options": [{"t": "Dear Sunita Madam,", "hi": "आदरणीय", "ok": True},
		                                            {"t": "Hey you,", "hi": "ऐ", "ok": False}]}]})}],
		               }).insert(ignore_permissions=True)
		api.clear_content_cache()
		t = next(c for c in api._build_courses() if c["key"] == key)
		les = t["lessons"][0]
		self.assertEqual(len(les["reply"]), 1)
		r = les["reply"][0]
		self.assertEqual(r["from"], "Sunita Madam")
		self.assertEqual(r["subject"], "Class time")
		self.assertEqual(r["msg"], "Class starts at 10 tomorrow.")
		self.assertEqual(len(r["slots"]), 1)
		self.assertTrue(r["slots"][0]["options"][0]["ok"])

	def test_reply_export_survives_bad_spec_json(self):
		key = "reply-badspec-t"
		def _rm():
			frappe.db.delete("Lesson", {"track": key})
			frappe.db.delete("Track", {"name": key})
			frappe.db.commit()
			api.clear_content_cache()
		self.addCleanup(_rm)
		if frappe.db.exists("Track", key):
			frappe.delete_doc("Track", key, force=1, ignore_permissions=True)
		track = frappe.get_doc({"doctype": "Track", "track_key": key, "title": "Reply B",
		                        "published": 1}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Lesson", "track": track.name, "lesson_key": "l1",
		                "title": "L1", "published": 1,
		                "reply": [{"from_name": "X", "message": "m", "spec_json": "not-json["}],
		               }).insert(ignore_permissions=True)
		api.clear_content_cache()
		t = next(c for c in api._build_courses() if c["key"] == key)
		r = t["lessons"][0]["reply"][0]
		self.assertEqual(r["slots"], [])   # malformed spec → empty slots; game skips the round
