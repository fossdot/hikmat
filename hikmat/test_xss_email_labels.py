# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Regression tests for the Lesson Email slot label stored-XSS fix.

The vulnerable interpolations in roundEmail() (game.html) used
`tx(s.label, s.labelHi || s.label)` to render teacher-authored slot
labels into innerHTML. `tx()` is a passthrough language switch; it does
not escape. A malicious `spec_json` could inject <script> / <img onerror>
/ etc. that ran under the game's origin.

The fix wraps both interpolations with `esc()`. These tests guard the
fix in two layers:

1. Source-level regression — assert game.html contains the patched
   `esc(tx(s.label, ...))` form at the two known sites, and contains
   no remaining unescaped `tx(s.label, ...)` call. This is a cheap
   grep-style guard against the wrapper being removed by a future
   editor.

2. Behaviour — assert the `esc()` function (mirrored from game.html in
   Python here for hermetic testing) neutralises the attack payloads
   listed in the issue, and round-trips legitimate strings (English,
   Hindi devanagari, mixed) unchanged.

Run with either:
    python -m unittest hikmat.test_xss_email_labels
    bench --site <site> run-tests --app hikmat --module hikmat.test_xss_email_labels
"""
import re
import unittest
from pathlib import Path

GAME_HTML = Path(__file__).parent / "public" / "game.html"


def esc(s):
	"""Python mirror of `function esc(s)` in hikmat/public/game.html.

	Same replacements, same order (& first, then <, then >). Kept here so
	this test file is hermetic — no browser / no JS engine required to run.
	If the game.html implementation drifts, update this mirror and add a
	test for the new behaviour.
	"""
	if s is None:
		s = ""
	return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TestPatchedTemplatePresence(unittest.TestCase):
	"""Source-level guard: both interpolations must wrap with esc(...)."""

	def setUp(self):
		self.src = GAME_HTML.read_text(encoding="utf-8")

	def test_both_sites_are_escaped(self):
		patched = re.findall(
			r"esc\(\s*tx\(s\.label,\s*s\.labelHi\s*\|\|\s*s\.label\)\s*\)",
			self.src,
		)
		# Two known sites: <span class="elabel"> and <div class="oglabel">.
		self.assertEqual(
			len(patched), 2,
			f"Expected exactly 2 esc(tx(s.label, ...)) sites in game.html; found {len(patched)}.",
		)

	def test_no_unescaped_site_remains(self):
		# Any tx(s.label, …) NOT preceded by `esc(` is a regression.
		# Use a manual scan instead of variable-length lookbehind for portability.
		for m in re.finditer(r"tx\(s\.label,\s*s\.labelHi\s*\|\|\s*s\.label\)", self.src):
			start = m.start()
			prefix = self.src[max(0, start - 4):start]
			self.assertEqual(
				prefix, "esc(",
				f"Unescaped tx(s.label, …) at byte {start}: prefix was {prefix!r}.",
			)


class TestEscFunction(unittest.TestCase):
	"""Behaviour guard: esc() must neutralise the documented attack payloads."""

	# --- documented attack payloads ---

	def test_neutralises_script_tag(self):
		out = esc("<script>alert(1)</script>")
		self.assertNotIn("<script>", out)
		self.assertEqual(out, "&lt;script&gt;alert(1)&lt;/script&gt;")

	def test_neutralises_img_onerror(self):
		# Matches the issue body's PoC payload.
		out = esc('<img src=x onerror="alert(\'XSS\')">')
		self.assertNotIn("<img", out)
		self.assertTrue(out.startswith("&lt;img"))

	def test_neutralises_svg_onload(self):
		out = esc("<svg onload=alert(1)>")
		self.assertNotIn("<svg", out)
		self.assertEqual(out, "&lt;svg onload=alert(1)&gt;")

	def test_neutralises_iframe_src(self):
		out = esc("<iframe src='javascript:alert(1)'></iframe>")
		self.assertNotIn("<iframe", out)
		self.assertNotIn("</iframe>", out)

	# --- edge cases ---

	def test_passes_through_safe_english(self):
		for safe in ("Greeting", "From — sender", "Hello world", "Subject line"):
			self.assertEqual(esc(safe), safe)

	def test_passes_through_devanagari(self):
		# Legitimate Hindi labels must round-trip unchanged for the labelHi path.
		for safe in ("अभिवादन", "नमस्ते", "विषय", "हस्ताक्षर"):
			self.assertEqual(esc(safe), safe)

	def test_passes_through_emoji_and_mixed_unicode(self):
		# Curriculum is allowed to contain emoji and mixed scripts.
		self.assertEqual(esc("🌟 Subject 🌸"), "🌟 Subject 🌸")
		self.assertEqual(esc("Hello नमस्ते 🙂"), "Hello नमस्ते 🙂")

	def test_ampersand_escaped_first_no_double_encode(self):
		# Order of replacement matters; & must be replaced first so that the
		# subsequent < > replacements don't introduce a re-encoded entity.
		self.assertEqual(esc("Tom & Jerry"), "Tom &amp; Jerry")
		# A pre-encoded entity gets re-encoded (expected — esc is a one-way
		# pass, not idempotent on already-encoded text). This matches the JS.
		self.assertEqual(esc("&amp;"), "&amp;amp;")

	def test_handles_null_and_empty(self):
		# game.html's esc coerces null/undefined to "" via String(s==null?"":s).
		self.assertEqual(esc(None), "")
		self.assertEqual(esc(""), "")

	def test_handles_non_string_input(self):
		# A teacher could put a JSON number in spec_json; esc must coerce.
		self.assertEqual(esc(0), "0")
		self.assertEqual(esc(42), "42")

	def test_quotes_pass_through_unchanged(self):
		# These two sites render label as text-content (between element tags),
		# not as an attribute value, so esc() is the right tool. Quote
		# characters are NOT escaped — confirm that's the documented behaviour
		# (escAttr exists separately for attribute contexts).
		self.assertEqual(esc('he said "hi"'), 'he said "hi"')
		self.assertEqual(esc("it's fine"), "it's fine")


if __name__ == "__main__":
	unittest.main()
