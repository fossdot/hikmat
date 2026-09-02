# Bodhya Learn — Security & Deploy Checklist

> **Note (2026-08-29): this document describes the BACKEND and the WEB app, both unchanged.**
> Accounts were removed from the **Android** build on 2026-08-27, so the packaged app no longer
> exercises any of the authentication, token, rate-limiting or roster surfaces below — it makes three
> unauthenticated content GETs and nothing else. None of the controls here are obsolete: they still
> protect learn.bodhya.net/play and the facilitator Desk, which keep accounts. Treat this file as
> covering the server and the web client, not the Play Store app.


Bodhya Learn stores data about **minors**. Treat it accordingly. This is the operational
checklist that complements the in-code hardening (PIN hashing, login tokens, rate
limits, input validation, cached read APIs).

## Before going live on a public host (e.g. Frappe Cloud)

- [ ] **Strong Administrator password.** Set a long, unique password during site
      creation. Never ship the dev default (`admin123`). Rotate any bootstrap value.
- [ ] **Enable two-factor auth for Desk.** Frappe → *System Settings* → enable
      Two Factor Authentication (at least for System Manager accounts).
- [ ] **Minimise System Manager / facilitator accounts.** The Desk (`/app`) is the admin
      surface; only staff who need it get a Desk login (+ 2FA). Campus students are
      **not** Frappe Users; online students are login-only **Website Users**
      (no email, no Desk) — see the enrolment model below.
- [ ] **HTTPS only** (Frappe Cloud does this automatically). Required for the PWA and
      for protecting login tokens in transit.
- [ ] **Verify the client IP the app derives** — see *Client IP / trusted proxies* below.
      Zero configuration is correct for the two common topologies, but a managed host or
      CDN in front needs its ranges added, and **nothing else in the app can tell you it
      is wrong**.
- [ ] *(Optional)* IP-allowlist / VPN-gate `/app` if the centre uses fixed locations.

## Client IP / trusted proxies (rate limits + login lockout depend on it)

Every per-IP ceiling — and the per-source half of the PIN lockout — is keyed on the address
the app derives for the caller. `X-Forwarded-For` is client-supplied text, so it is believed
**only when the request's socket peer is itself a trusted proxy**; then the chain is walked
**right to left** (nginx *appends* what it saw), trusted addresses are skipped, and the first
untrusted entry is the client. Otherwise the header is ignored and the socket peer is used.

- **Default trusted set (no config needed):** loopback + RFC1918 + link-local + IPv6
  unique-local — exactly where a reverse proxy of ours can sit. A **public** address is
  never trusted by default. This is already correct for:
  - a **directly exposed** app (no proxy): the header is ignored, so it cannot be spoofed;
  - **one nginx on localhost** (standard bench / Frappe Cloud site): the rightmost
    non-private entry is the real client.
- **`hikmat_trusted_proxies`** (site config) overrides the set — a list of IPs/CIDRs, e.g.
  `["203.0.113.0/24", "2001:db8::/32"]`. Add the egress ranges of anything **public** in
  front of the site (a CDN, an external load balancer, a managed platform's edge). An
  **empty list** means trust nobody, i.e. ignore `X-Forwarded-For` entirely — that is what
  the dev bench sets, because gunicorn there listens on localhost and localhost would
  otherwise be a trusted peer.
- **`hikmat_trusted_proxy_hops` is obsolete** (it was a hop *count*, wrong in both
  directions and invisible either way). If a site still carries it, the app logs an error
  saying so. Remove it.
- **What an operator must actually verify, once, per deployment:** open
  `/api/method/hikmat.api.whoami_ip` from a device whose public IP you know, logged in as a
  System Manager. `client_ip` **must equal that device's public IP**.
  - If it comes back as a **proxy/edge address** (every client would then share one rate
    limit bucket — with the fail-closed signup/capture ceilings that can lock a site out),
    add that proxy's range to `hikmat_trusted_proxies`.
  - If it echoes a value **you can set yourself** with a spoofed `X-Forwarded-For`, the peer
    is being trusted when it should not be: set the list to `[]`.
- **Self-diagnosis:** when the peer is trusted but the whole chain is trusted or unreadable
  (so no client address exists), the app logs an ERROR naming `hikmat_trusted_proxies` in
  `logs/hikmat.log`. Grep that file after go-live. A LAN deployment where the learners'
  devices are themselves on private addresses is the normal case for this warning — fix it
  by trusting **only** the proxy (`["127.0.0.1"]`), so the LAN clients stay distinct.
- Ceilings are deliberately generous because a whole classroom NATs to one IP: measured
  headroom is ≥166 girls/hour on every ceiling except **signup (60/hour)**, which is the
  tightest. A 30-girl class enrolling at once uses half of it; a 60+ girl mass sign-up
  behind one IP would be refused (`rate_limited`, retried by the client) — stagger it, or
  create campus learners in Desk, which does not go through signup.

## Student auth model (already in code)

- A **PIN is required** for every profile (4–8 digits) and verification is
  **fail-closed** — a PIN-less profile cannot be logged in (closes the old shared-laptop
  hole where a PIN-less profile opened with zero auth). PINs are **hashed**
  (`pbkdf2:sha256`); legacy plaintext upgrades on next login.
- Short numeric PINs are fine because login has a **per-account lockout**, and PINs only
  separate kids on shared laptops. The lockout is keyed on the **account** — the Student
  docname for `login_student`, the typed name for `login_by_name` — and **never** on the
  client IP: it used to include the IP, which made it worthless (eight wrong PINs from
  eight spoofed `X-Forwarded-For` values never tripped it, so a 4-digit PIN was
  brute-forceable). Two budgets, both cleared by a successful login:
  - **8 wrong PINs → 5-minute cooldown** (unchanged), which lifts by itself;
  - **50 wrong PINs in 24h → the profile parks.** This is what actually defeats brute force
    (~200 days to cover half a 4-digit space, versus ~2 days on the 5-minute counter alone).
  A per-source ceiling of **600 failed logins/hour** limits spraying many accounts from one
  place; it counts failures only, so normal logins never touch it.
  Note `login_by_name` can only key on the name it was given, and **names are not unique** —
  two girls called *Asha* share that budget (the name is hashed into the cache key, so no
  child's name sits in Redis). If a duplicate name ever causes trouble, clear it with
  `clear_login_lockout(name="Asha")` and give one of them a distinct display name.
- **The trade-off, on purpose:** a per-account lockout means someone can deliberately lock a
  child out of her own profile. It is priced down as far as it goes — generous budgets (a
  girl mistyping two or three times is nowhere near them), a bounded 24h window, and a
  facilitator release valve: **`clear_login_lockout(student=…)`** (or `name=…`, or `ip=…`),
  System-Manager only, which also reports how many failures it found so you can tell a real
  lockout from a girl who keeps mistyping. A **provisioned campus laptop is unaffected
  either way** — it verifies her PIN on-device against the cached roster hash and never
  calls these endpoints, so the lockout only binds the typed-name/online path.
- Each student gets a per-student **token** at login/signup, required by
  `submit_attempt` / `get_progress`. Tokens **expire after 90 days** (sliding window — an
  active login refreshes it) and **rotate** when missing/expired. A facilitator can force
  re-login everywhere with **`revoke_student_token(student)`** (Desk-only), e.g. for a
  lost or handed-down laptop.
- **Unticking `active` on a Student now cuts off her device immediately**, on every
  endpoint — it is checked in the shared auth path, not per endpoint. (It used to gate
  only new logins and one write path, so a deactivated girl's cached token kept working
  for up to 90 days unless someone also called `revoke_student_token`.) Deactivate to
  offboard; `revoke_student_token` is for rotating a token on an account that stays live.
  **Withdrawn consent:** deactivate for an immediate stop, then `delete_student` to erase
  what is already stored — deactivation alone deletes nothing.
- Login is **by name + PIN** (`login_by_name`, indexed lookup) — the roster is never
  listed publicly and errors are generic (no "does this name exist?" enumeration).
- Self-signups still require a guardian/teacher **consent** acknowledgement in the UI.

## Enrolment model (intake batch + campus)

- A **Cohort** is a start-date **intake batch** (e.g. "Aug 2026", `start_date`), not a
  physical centre; a batch can hold both campus and online learners.
- Each **Student** has `mode` (**Campus** / **Online**), an optional `campus`
  (Link → Campus, e.g. *Noor Girls High School, Meghwal Mathia*), and `user`
  (Link → Frappe User, online learners only).
- **Campus (offline)** learners log in on shared laptops from a cached roster with an
  on-device PIN check. **Online** learners (Phase 2) self-register with a per-cohort
  **invite code** and log in as a login-only Website User (username + PIN, no email).

## Help / doubts — routed to a facilitator (AI tutor deferred)

- The AI voice tutor (Roshni) is **deferred for now**. A learner's "I'm stuck / help" tap
  logs a **Lesson Doubt** for the teacher in **Desk** (the Confusion Heatmap report).
  Actively **notifying** the facilitator in Desk on each new doubt is the next small step
  — there is no student-facing bot.

## Children's data — retention & erasure

- Lesson Attempt rows denormalise `student_name`/`cohort` for reporting and grow over
  time. To erase a child's data, use **`hikmat.api.delete_student(student)`**
  (facilitator/System-Manager only) — it cascades over her attempts, tests, doubts,
  events, attendance, evaluations, AI chats, and the Frappe User of an online learner.
  It also scrubs the bookkeeping rows Frappe leaves behind naming her (delete-feed
  Comments, Versions, Notification Logs, DocShares), which its own queued cleanup does
  not reach on a site with no worker. A half-finished erasure is **resumed** rather than
  refused, so a run interrupted part-way can simply be re-run.
- **The app no longer records audio.** The Bhojpuri AI ("Boli") corpus feature — recording,
  transcription, verification, the speaker registry and the XP ledger — was **removed
  outright**, and the `v12_remove_boli` migration erases everything it had collected:
  the rows, the doctypes and tables behind them, and the private audio bytes on disk.
  Nothing in the app asks for microphone access any more.
- **Erasing one girl never costs another one anything.** Erasure is scoped to rows keyed
  on her id; a peer's rows are never touched.
- **Decide and document a retention window** (e.g. purge inactive students' attempts
  after N years) before a full rollout. A scheduled job can be added to
  `scheduler_events` in `hooks.py`.
- Keep a short **privacy notice** for parents/teachers describing what is stored
  (first name/nickname, avatar, progress) and how to request deletion.

## Production cutover checklist (day-one, in order)

A fresh install (`bench new-site` + `install-app hikmat`, or a Frappe Cloud deploy)
runs `after_install` which seeds content, belts, the campus, the two cohorts
(**Online** / **NGHS Sept-2026**), a **random invite code**, and the login System
Settings (username login on, password policy off). Patches do NOT run on fresh
installs — everything they seed is mirrored in `seed_operational_defaults()`.

1. **Admin password**: set a strong, unique Administrator password; enable **2FA**
   for every System Manager (Desk → Two Factor Authentication settings).
2. **Invite code**: read the generated code from the *Online* cohort (Desk → Cohorts)
   and share it only through the facilitator channel. Rotate it there any time it leaks.
3. **Facilitator accounts**: one named Desk user per facilitator (System Manager for
   now — a scoped role is future hardening); no shared logins.
4. **Learner data**: if a copied/dev database is ever promoted, run
   `bench --site <site> execute hikmat.setup_data.wipe_demo_data` so real analytics
   start from zero (content, belts, cohorts and settings survive the wipe).
5. **Campus laptop provisioning** (offline path): a facilitator opens the game →
   ⚙️ Set-up → logs in → picks the campus → the roster caches on-device → log out.
   Verify a girl can then log in by name + PIN with wifi OFF.
6. **Smoke test** (5 min): guest plays a lesson → 2-lesson wall appears → online
   signup with the invite code works → Desk shows the attempt, and the Trouble
   Spots / Drill-down reports fill in.
7. **Backups**: on Frappe Cloud they're automatic; self-hosted, schedule
   `bench --site <site> backup` off-machine.

## Reporting

Found a vulnerability? Email **vishal@fossunited.org** — please don't open a public
issue for security problems.
