app_name = "hikmat"
app_title = "Hikmat"
app_publisher = "FOSS United"
app_description = "Game-style English learning courses for girls in Champaran"
app_email = "vishal@fossunited.org"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "hikmat",
# 		"logo": "/assets/hikmat/logo.png",
# 		"title": "Hikmat",
# 		"route": "/hikmat",
# 		"has_permission": "hikmat.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/hikmat/css/hikmat.css"
# app_include_js = "/assets/hikmat/js/hikmat.js"

# include js, css files in header of web template
# web_include_css = "/assets/hikmat/css/hikmat.css"
# web_include_js = "/assets/hikmat/js/hikmat.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "hikmat/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "hikmat/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "hikmat.utils.jinja_methods",
# 	"filters": "hikmat.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "hikmat.install.before_install"
after_install = "hikmat.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "hikmat.uninstall.before_uninstall"
# after_uninstall = "hikmat.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "hikmat.utils.before_app_install"
# after_app_install = "hikmat.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "hikmat.utils.before_app_uninstall"
# after_app_uninstall = "hikmat.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "hikmat.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Bust the cached read endpoints (get_courses/get_structure/get_settings) whenever
# content is edited in Desk, so teacher changes show up without waiting for a TTL.
_bust = {"on_update": "hikmat.api.clear_content_cache", "on_trash": "hikmat.api.clear_content_cache"}
doc_events = {
	"Track": _bust,
	"Lesson": _bust,
	"Dialogue": _bust,
	"Grade Band": _bust,
	"Subject": _bust,
	"Hikmat Settings": {"on_update": "hikmat.api.clear_content_cache"},
	# Milestone thresholds ride in the cached settings payload → bust on edit.
	"Hikmat Milestone": _bust,
	# Module-test banks ride inside the cached courses payload → bust on edit.
	# (Child-table question edits save the parent, so the parent hook covers them.)
	"Module Test": _bust,
	# Stamp who/when a facilitator recorded an evaluation outcome in Desk.
	"Evaluation": {"before_save": "hikmat.api.stamp_evaluation"},
	# Offline cohorts must carry a start date (server-side twin of mandatory_depends_on).
	"Cohort": {"validate": "hikmat.api.validate_cohort"},
}

# Scheduled Tasks
# ---------------

# Raw attendance pings are an idempotency/audit ledger — the Day aggregates are the
# permanent record. 90-day retention comfortably exceeds every client-side horizon.
scheduler_events = {
	"daily": [
		"hikmat.api.prune_attendance_pings",
	],
}

# scheduler_events = {
# 	"all": [
# 		"hikmat.tasks.all"
# 	],
# 	"daily": [
# 		"hikmat.tasks.daily"
# 	],
# 	"hourly": [
# 		"hikmat.tasks.hourly"
# 	],
# 	"weekly": [
# 		"hikmat.tasks.weekly"
# 	],
# 	"monthly": [
# 		"hikmat.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "hikmat.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "hikmat.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "hikmat.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["hikmat.utils.before_request"]
# Content-Security-Policy — see set_security_headers() at the bottom of this file.
after_request = ["hikmat.hooks.set_security_headers"]

# Job Events
# ----------
# before_job = ["hikmat.utils.before_job"]
# after_job = ["hikmat.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"hikmat.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []


# ---------------------------------------------------------------------------
# Content-Security-Policy  (security audit finding C2 — defence in depth)
# ---------------------------------------------------------------------------
# WHY THIS EXISTS: Desk writes report/list cell values into the DOM with
# `innerHTML` (frappe-datatable cellmanager.js), so any student free text that
# ever slips past server-side escaping executes in the facilitator's session.
# Escaping the values is the primary fix; this header is the second line of
# defence so that a *future* missed escape is not automatically an account
# takeover.
#
# Frappe 15 ships no CSP of its own (the only occurrence in the framework is
# Web Form's frame-ancestors header) and exposes no CSP setting, so we attach
# ours with the framework's own `after_request` hook rather than bolting a
# bespoke WSGI/response filter onto the app.
#
# The policies live in this file, next to the hook that registers them, so that
# the whole control is reviewable in one place.
#
# NOT COVERED: `/assets/**` (so `/assets/hikmat/game.html`) and public `/files/**`
# never reach this hook — they are served by werkzeug's SharedData/StaticData
# middleware in dev and straight off nginx in production. `/private/files/**` does
# reach it, because that path is served by the app itself. The uncovered /assets
# path is why the game (one self-contained file full of inline <style>/<script>)
# keeps working untouched. If an operator ever adds a CSP for /assets at the nginx
# layer it MUST allow 'unsafe-inline' for both script-src and style-src and allow
# https://www.youtube.com in frame-src, or the game and its explainer videos break.

#: Enforced on every HTML response. Deliberately narrow: each directive below was
#: checked against Frappe 15's Desk before being switched on. Nothing else is
#: named, and `default-src` is deliberately absent, so no other resource type is
#: restricted and nothing can break by omission.
CSP_ENFORCED = (
	# Desk has no <base> tag. An injected one would silently repoint every
	# relative URL — including /api/method calls — at an attacker's host.
	"base-uri 'self'; "
	# No plugin content anywhere in Desk. (app_icon.js wraps inline SVG in an
	# <object> with no data/src attribute, which fetches nothing and so is not
	# gated by object-src — verified in a headless browser.)
	"object-src 'none'; "
	# Stops injected markup from POSTing the facilitator's data to a foreign
	# origin. Frappe submits everything over XHR; no template in frappe or
	# hikmat posts a form cross-origin.
	"form-action 'self'; "
	# Frappe sets no X-Frame-Options at all, so this is the only anti-clickjacking
	# control on Desk. Safe for the game: it embeds YouTube, it is never embedded.
	"frame-ancestors 'self'"
)

#: REPORT-ONLY, NOT ENFORCED — and that is a deliberate, documented compromise.
#:
#: `script-src-attr 'none'` is the one directive that would actually neutralise
#: C2's payload: the injected value lands in a datatable cell via innerHTML, and
#: innerHTML never runs <script>, so the exploit is always an inline event
#: handler (`<img src=x onerror=…>`, `<svg onload=…>`). script-src-attr blocks
#: exactly those, while leaving Desk's inline <script> blocks and its eval-based
#: template compiler alone (script-src-elem/eval are not touched).
#:
#: It is not enforced because Frappe's own Desk executes inline handler
#: attributes, and blocking them breaks controls the facilitator needs:
#:   * every navbar dropdown Action item, INCLUDING "Log out"
#:     (frappe/hooks.py:479 standard_navbar_items → navbar.html:101
#:      `onclick="return {{ item.action }}"`)
#:   * the awesomebar's `onsubmit="return false;"` (navbar.html:13) — Enter would
#:     submit the search form and reload the page
#:   * `onclick="return false;"` on sidebar/dropdown anchors with href="#"
#:     (page.js:667, list_sidebar_group_by.js:87,232, list_sidebar_stat.html:10)
#:   * printview.html:17 `onclick="window.print()"`, form_footer.html:6
#:     scroll-to-top, form.js:1123 "document was modified → Refresh"
#: A CSP that logs a facilitator out of her own tools — on a shared device in
#: Champaran, with no admin nearby — is worse than no CSP, so it ships as
#: report-only: violations appear in the browser console (and at a collector, if
#: an operator later adds a report-uri) without breaking a single control.
#:
#: Path-scoping is NOT a way out: Desk is a single-page app, so whichever policy
#: arrives with the first /app document governs every screen visited afterwards.
#: Enforcing it needs an upstream Frappe fix, or an 'unsafe-hashes' allowlist of
#: those handler bodies — which would silently break Desk on the next Frappe
#: upgrade, so that stays an explicit operator decision (see below).
CSP_REPORT_ONLY = "script-src-attr 'none'"


def set_security_headers(response=None, **kwargs):
	"""Attach the CSP headers above to HTML responses (`after_request` hook).

	Both policies can be overridden per site without a code deploy, which is how
	an operator flips the report-only experiment above into enforcement (or backs
	out of a policy that broke something) from the hosting dashboard alone:

	    site_config.json:
	      "hikmat_csp": "<policy>"                    # replaces CSP_ENFORCED
	      "hikmat_csp_report_only": "<policy>"        # replaces CSP_REPORT_ONLY
	      "hikmat_csp_report_only": ""                # sends no report-only header

	An empty string disables that header; omit the key to keep the default.
	"""
	import frappe

	# HTTPException paths (404, redirects raised as exceptions) hand us either no
	# response at all or an object with no werkzeug headers. Never raise from here:
	# frappe logs a hook failure and moves on, so a crash would be invisible.
	headers = getattr(response, "headers", None)
	if headers is None:
		return

	# Only documents can execute injected markup. Restricting to text/html also
	# means served HTML (e.g. /private/files/x.html) is covered while JSON API
	# replies and file downloads are left completely alone.
	if not (headers.get("Content-Type") or "").startswith("text/html"):
		return

	conf = getattr(frappe.local, "conf", None) or {}

	enforced = conf.get("hikmat_csp", CSP_ENFORCED)
	report_only = conf.get("hikmat_csp_report_only", CSP_REPORT_ONLY)

	# Never overwrite a policy the framework already set: Web Form pages emit
	# their own `frame-ancestors <allowed embedding domains>`, and a second,
	# stricter header would be intersected with it and silently break embedding.
	if enforced and "Content-Security-Policy" not in headers:
		headers["Content-Security-Policy"] = enforced

	if report_only and "Content-Security-Policy-Report-Only" not in headers:
		headers["Content-Security-Policy-Report-Only"] = report_only

