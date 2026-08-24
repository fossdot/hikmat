# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Make guardian phone verification available on an already-installed site.

Almost nothing here is schema work: `bench migrate` syncs the two new doctypes (Hikmat OTP,
Hikmat Consent) and the new Student / Hikmat Settings fields straight from their JSON, and
the new Student columns want exactly the defaults they ship with (guardian_verified = 0 —
nobody is retroactively consented, which is the only honest starting state). So there is
nothing to backfill and this patch deliberately does not invent anything.

What it DOES fix is the cache. get_settings() is served from Redis under SETTINGS_CACHE_KEY
with a one-hour TTL and is busted only by a content edit, so straight after a deploy every
client would keep receiving the OLD settings payload — without otpEnabled or the consent
wording — for up to an hour. The game reads `otpEnabled` to decide whether to show the
guardian gate at all, so the feature would appear to do nothing on a freshly migrated site
and then switch itself on later, which is precisely the kind of "did the deploy work?"
ambiguity that costs an evening. Busting it here makes the deploy deterministic.

The doctypes are reloaded explicitly for the same reason: this app is deployed from the
Frappe Cloud dashboard with no shell available, so a patch that assumes a particular
sync order is a patch that cannot be rescued by hand if it guesses wrong.

Left OFF: the feature ships disabled (otp_enabled = 0) and must be switched on in Desk once
the WhatsApp phone number ID and access token are filled in. That ordering is deliberate —
enabling it before the credentials exist would put a consent gate in front of every new
learner that cannot possibly send a code.
"""
import frappe


def execute():
    for dt in ("Hikmat OTP", "Hikmat Consent", "Student", "Hikmat Settings"):
        try:
            frappe.reload_doctype(dt)
        except Exception:
            # A doctype this app owns should always reload; if one cannot, the migrate that
            # follows will surface it far more clearly than a patch traceback would.
            frappe.log_error(title="v13_guardian_otp: reload failed for " + dt)

    from hikmat.api import clear_content_cache
    clear_content_cache()
    frappe.db.commit()
