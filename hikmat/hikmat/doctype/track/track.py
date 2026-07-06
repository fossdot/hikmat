# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class Track(Document):
	def validate(self):
		# Students play the game logged out of Frappe, so a /private/files video would
		# 403 for every one of them — fail at save time, not on the child's screen.
		for f in ("video", "video_captions", "video_captions_hi"):
			v = (self.get(f) or "").strip()
			if v.startswith("/private/"):
				frappe.throw(f"{self.meta.get_label(f)} must be a public file (uncheck "
				             "'Private' on the upload) — students are not logged in.")
		v = (self.video or "").strip().lower()
		if v and not v.endswith((".mp4", ".m4v")):
			frappe.msgprint("The video is not an .mp4 — some browsers (Safari) may not "
			                "play it. H.264 MP4 works everywhere.",
			                title="Video format", indicator="orange")
		for f in ("video_captions", "video_captions_hi"):
			c = (self.get(f) or "").strip().lower()
			if c and not c.endswith(".vtt"):
				frappe.msgprint(f"{self.meta.get_label(f)} should be a WebVTT (.vtt) file.",
				                title="Subtitles format", indicator="orange")
