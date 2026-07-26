# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Turn click/UI sound on by default on migrated sites.

The `default_sound` field on Hikmat Settings ships with a default of 1 ("Sound on by
default"), but the single settings doc on sites that predate the field was created without
it, so it reads 0. get_settings then returns defaultSound=false, and the game's
applyServerDefaults() force-mutes WebAudio (state.sound=false) for every profile that
hasn't manually toggled a preference — i.e. every freshly-logged-in student. (A guest that
switched language/theme set prefsTouched=true and so escaped the override, which is why the
bug looked like "sound works as guest, vanishes after login".)

Reassert the field's intended default where it's currently off. Save via the doc (not
db.set_value) so the on_update -> clear_content_cache hook fires and the cached
get_settings response is busted. One-shot: a facilitator who later turns sound off in Desk
is not overridden, because the patch is marked complete after it runs.
"""
import frappe


def execute():
    if not frappe.db.exists("DocType", "Hikmat Settings"):
        return
    ss = frappe.get_single("Hikmat Settings")
    if not ss.default_sound:
        ss.default_sound = 1
        ss.save(ignore_permissions=True)
        frappe.db.commit()
