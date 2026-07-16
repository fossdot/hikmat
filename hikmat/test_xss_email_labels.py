# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Regression tests for the Lesson Email slot label stored-XSS fix.

The vulnerable interpolations in roundEmail() (game.html) used
`tx(s.label, s.labelHi || s.label)` to render teacher-authored slot
labels into innerHTML. `tx()` is a passthrough language switch; it does
not escape. A malicious `spec_json` could inject <script> / <img onerror>
/ etc. that ran under the game's origin.

The fix wraps both interpolations with `esc()`. These tests guard the
fix in three layers:

1. Source-level regression — assert game.html contains the patched
   `esc(tx(s.label, ...))` form at the two known sites, and contains
   no remaining unescaped `tx(s.label, ...)` call. This is a cheap
   grep-style guard against the wrapper being removed.

2. Detector self-tests — the source-level guard is only useful if it
   accepts legitimate stylistic variation (whitespace, line breaks)
   and rejects subtler bypasses (wrong wrapper name, identifier-prefix
   tricks like `Xesc(`, partial fixes that escape one site but not
   the other). We exercise the detector against synthetic source
   strings so a future refactor doesn't silently weaken the guard.

3. Behaviour — assert the `esc()` function (mirrored from game.html in
   Python here for hermetic testing) neutralises the documented attack
   payloads and round-trips legitimate strings (English, Hindi
   devanagari, mixed) unchanged.

Run with either:
    python -m unittest hikmat.test_xss_email_labels
    bench --site <site> run-tests --app hikmat --module hikmat.test_xss_email_labels
"""
import re
import unittest
from pathlib import Path

GAME_HTML = Path(__file__).parent / "public" / "game.html"

# Matches the curriculum-content interpolation that was vulnerable in
# roundEmail(): tx(s.label, s.labelHi || s.label). Whitespace inside the
# call is tolerated.
_TX_SLOT_LABEL = re.compile(
	r"tx\(s\.label,\s*s\.labelHi\s*\|\|\s*s\.label\)"
)

# JS identifier-continuation char (letters, digits, underscore, $). Used to
# rule out wrapper names like `Xesc(`, `_esc(`, `$esc(`, etc., which would
# otherwise satisfy a naive 4-byte "esc(" suffix check.
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$]")


def _find_unescaped_tx_label_sites(src):
	"""Return [(match_start, surrounding_prefix), ...] for every
	`tx(s.label, s.labelHi || s.label)` call in `src` that is NOT wrapped
	by `esc(...)`.

	The check walks left from each match start, skipping whitespace
	(including newlines), and requires:

	* the next four characters before the whitespace to be exactly `esc(`,
	  AND
	* the character before those four, if present, to NOT be an identifier
	  character (so `Xesc(`, `_esc(`, `myEsc(` etc. are all rejected).

	An empty return list means every interpolation is guarded.
	"""
	bad = []
	for m in _TX_SLOT_LABEL.finditer(src):
		i = m.start()
		while i > 0 and src[i - 1].isspace():
			i -= 1
		prefix4 = src[max(0, i - 4):i]
		preceding = src[i - 5] if i - 5 >= 0 else ""
		ok = prefix4 == "esc(" and not (preceding and _IDENT_CHAR.match(preceding))
		if not ok:
			# Include 10 chars of left context so a failure message is debuggable.
			bad.append((m.start(), src[max(0, i - 10):i]))
	return bad


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
	"""Source-level guard against the actual game.html shipped in this repo."""

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
		bad = _find_unescaped_tx_label_sites(self.src)
		self.assertEqual(
			bad, [],
			f"Found {len(bad)} unescaped tx(s.label, …) site(s): {bad}",
		)


class TestUnescapedSiteDetector(unittest.TestCase):
	"""Adversarial tests for `_find_unescaped_tx_label_sites` itself.

	The source-level guard is only as good as its detector. These tests
	pin the detector's accept / reject behaviour so a future refactor can't
	silently weaken it.
	"""

	# ---- accepted: legitimate stylistic variation ----

	def test_accepts_canonical(self):
		self.assertEqual(
			_find_unescaped_tx_label_sites("esc(tx(s.label, s.labelHi || s.label))"),
			[],
		)

	def test_accepts_space_after_open_paren(self):
		# The Copilot review's specific concern: `esc( tx(...))` is harmless.
		self.assertEqual(
			_find_unescaped_tx_label_sites("esc( tx(s.label, s.labelHi || s.label))"),
			[],
		)

	def test_accepts_tab_after_open_paren(self):
		self.assertEqual(
			_find_unescaped_tx_label_sites("esc(\ttx(s.label, s.labelHi || s.label))"),
			[],
		)

	def test_accepts_newline_after_open_paren(self):
		# Multi-line wrapping is common when reformatting.
		self.assertEqual(
			_find_unescaped_tx_label_sites(
				"esc(\n\ttx(s.label, s.labelHi || s.label))"
			),
			[],
		)

	def test_accepts_multiple_sites(self):
		src = (
			"esc(tx(s.label, s.labelHi || s.label)) AND "
			"esc(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(_find_unescaped_tx_label_sites(src), [])

	def test_accepts_no_occurrences(self):
		# A source with no tx(s.label, …) at all is vacuously clean.
		self.assertEqual(_find_unescaped_tx_label_sites("nothing to see here"), [])

	def test_accepts_leading_whitespace_before_esc(self):
		# `esc(...)` at position 0 with leading spaces in a multi-line template.
		self.assertEqual(
			_find_unescaped_tx_label_sites(
				"  esc(tx(s.label, s.labelHi || s.label))"
			),
			[],
		)

	# ---- rejected: missing / wrong wrapper ----

	def test_rejects_bare_tx(self):
		out = _find_unescaped_tx_label_sites("tx(s.label, s.labelHi || s.label)")
		self.assertEqual(len(out), 1, out)

	def test_rejects_wrong_wrapper_escAttr(self):
		# escAttr exists in the codebase for attribute contexts; it is the
		# wrong tool for these two text-context sites and the test must flag it.
		out = _find_unescaped_tx_label_sites(
			"escAttr(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(len(out), 1, out)

	def test_rejects_arbitrary_wrappers(self):
		for wrapper in ("identity", "JSON.stringify", "foo", "bar", "escape"):
			src = f"{wrapper}(tx(s.label, s.labelHi || s.label))"
			out = _find_unescaped_tx_label_sites(src)
			self.assertEqual(
				len(out), 1,
				f"detector should reject `{wrapper}(tx(...))`; got {out!r}",
			)

	def test_rejects_identifier_prefix_X_esc(self):
		# `Xesc(` happens to end in the same 4 bytes `esc(` as a real wrapper.
		# Must NOT pass — `Xesc` is a different function name.
		out = _find_unescaped_tx_label_sites(
			"Xesc(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(len(out), 1, out)

	def test_rejects_identifier_prefix_underscore(self):
		out = _find_unescaped_tx_label_sites(
			"_esc(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(len(out), 1, out)

	def test_rejects_identifier_prefix_dollar(self):
		# `$` is a valid JS identifier character.
		out = _find_unescaped_tx_label_sites(
			"$esc(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(len(out), 1, out)

	def test_rejects_camelcase_my_esc(self):
		out = _find_unescaped_tx_label_sites(
			"myEsc(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(len(out), 1, out)

	def test_rejects_dot_access(self):
		# `esc.tx(...)` is method-call syntax, not the escape wrapper.
		out = _find_unescaped_tx_label_sites(
			"esc.tx(s.label, s.labelHi || s.label)"
		)
		self.assertEqual(len(out), 1, out)

	def test_rejects_concatenation_bypass(self):
		# An adversary who has read the Copilot review's count-equality
		# suggestion (b) might try `esc(other) + tx(s.label, …)` — the
		# counts of `tx(s.label, …)` and `esc(tx(s.label, …))` can be made
		# to look balanced, but this site is still unescaped. Our detector
		# binds each tx() to its own wrapper, so it catches it.
		src = "esc(other) + tx(s.label, s.labelHi || s.label)"
		out = _find_unescaped_tx_label_sites(src)
		self.assertEqual(len(out), 1, out)

	def test_rejects_partial_fix_one_of_two(self):
		# Two sites — one escaped, one missed. The detector must flag exactly
		# the missed one.
		src = (
			"esc(tx(s.label, s.labelHi || s.label)) AND "
			"tx(s.label, s.labelHi || s.label)"
		)
		out = _find_unescaped_tx_label_sites(src)
		self.assertEqual(len(out), 1, out)

	def test_rejects_case_variant_Esc(self):
		# JS is case-sensitive; `Esc(` is a different function. The test
		# being case-sensitive also forces a future rename of esc() to
		# update the test in lockstep.
		out = _find_unescaped_tx_label_sites(
			"Esc(tx(s.label, s.labelHi || s.label))"
		)
		self.assertEqual(len(out), 1, out)


class TestEscFunction(unittest.TestCase):
	"""Behaviour guard: esc() must neutralise the documented attack payloads."""

	# ---- documented attack payloads ----

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

	# ---- edge cases ----

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
