# Hikmat — Security & Deploy Checklist

Hikmat stores data about **minors**. Treat it accordingly. This is the operational
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
- [ ] *(Optional)* IP-allowlist / VPN-gate `/app` if the centre uses fixed locations.

## Student auth model (already in code)

- A **PIN is required** for every profile (4–8 digits) and verification is
  **fail-closed** — a PIN-less profile cannot be logged in (closes the old shared-laptop
  hole where a PIN-less profile opened with zero auth). PINs are **hashed**
  (`pbkdf2:sha256`); legacy plaintext upgrades on next login.
- Short numeric PINs are fine because login has a per-student **lockout** (8 wrong tries
  → 5-min cooldown) and PINs only separate kids on shared laptops.
- Each student gets a per-student **token** at login/signup, required by
  `submit_attempt` / `get_progress`. Tokens **expire after 90 days** (sliding window — an
  active login refreshes it) and **rotate** when missing/expired. A facilitator can force
  re-login everywhere with **`revoke_student_token(student)`** (Desk-only), e.g. for a
  lost or handed-down laptop.
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
  (facilitator/System-Manager only) — it cascades the delete over their attempts.
- **Decide and document a retention window** (e.g. purge inactive students' attempts
  after N years) before a full rollout. A scheduled job can be added to
  `scheduler_events` in `hooks.py`.
- Keep a short **privacy notice** for parents/teachers describing what is stored
  (first name/nickname, avatar, progress) and how to request deletion.

## Reporting

Found a vulnerability? Email **vishal@fossunited.org** — please don't open a public
issue for security problems.
