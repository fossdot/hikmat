# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Register the packaged Android app's origin for CORS, so the game can READ our replies.

The Play Store build runs inside a Capacitor WebView whose origin is `https://localhost`
(capacitor.config.json sets androidScheme:"https"), while the backend is this site. Every
API call is therefore cross-site, and Frappe emits CORS headers only for origins listed in
`allow_cors` (frappe/app.py::set_cors_headers, exact string match).

WHY THIS WAS NOT MERELY "SYNC IS OFF". A form-encoded POST carrying no custom header is a
CORS *simple* request: the browser sends it without a preflight, so `signup_student` really
did reach us and really did create the Student — the app just could not read the reply. It
therefore never learned the id + bearer token we handed back, invented a device-local
profile instead, and signed every subsequent attempt with an id that does not exist here.
submit_attempt answered `unknown_student`, which the game treats as a permanent rejection
and drops. Writes landed, identity did not, and the activity data was discarded on-device.
Nine testers showed up in the Student list with zero attempts between them.

Set in code rather than left to the dashboard because this bench has no SSH: the config a
correct deployment needs should ride along with the code that needs it, not be a checklist
item someone re-does after every site rebuild.

NOT a security widening. It grants a named local app origin permission to read responses it
is already able to trigger; `allow_cors="*"` is what would be dangerous (and is refused by
browsers anyway once Access-Control-Allow-Credentials is set). Learner endpoints authorise
on a bearer token passed as a parameter (_token_ok), never on the caller's origin.

Idempotent, and MERGES rather than assigns: a site that already allows other origins keeps
them.
"""
import frappe
from frappe.installer import update_site_config

# The packaged Android WebView. An iOS build would add "capacitor://localhost", and
# `ionic cap serve` "http://localhost:8100" — deliberately not pre-listed: an allowlist
# should name origins that actually exist.
APP_ORIGINS = ["https://localhost"]


def execute():
    current = frappe.conf.get("allow_cors")

    if current == "*":
        # Already maximally open. Narrowing it here would be a behaviour change this patch
        # has no mandate for (and it is a config someone chose), so leave it and say so.
        print("=== v15: allow_cors is '*'; leaving it alone (already permits the app) ===")
        return

    if isinstance(current, str):
        origins = [current] if current else []      # a bare string is a valid single origin
    elif isinstance(current, (list, tuple)):
        origins = list(current)
    else:
        origins = []

    missing = [o for o in APP_ORIGINS if o not in origins]
    if not missing:
        return

    update_site_config("allow_cors", origins + missing)
    frappe.conf.allow_cors = origins + missing      # so anything later in THIS migrate agrees
    print("=== v15: allow_cors now " + str(origins + missing) + " ===")
