# Hikmat — Security & Deploy Checklist

Hikmat stores data about **minors**. Treat it accordingly. This is the operational
checklist that complements the in-code hardening (PIN hashing, login tokens, rate
limits, input validation, cached read APIs).

## Before going live on a public host (e.g. Frappe Cloud)

- [ ] **Strong Administrator password.** Set a long, unique password during site
      creation. Never ship the dev default (`admin123`). Rotate any bootstrap value.
- [ ] **Enable two-factor auth for Desk.** Frappe → *System Settings* → enable
      Two Factor Authentication (at least for System Manager accounts).
- [ ] **Minimise System Manager accounts.** The Desk (`/app`) is the admin surface;
      only facilitators who need it should have a Desk login. Students are **not**
      Frappe Users — they never touch Desk.
- [ ] **HTTPS only** (Frappe Cloud does this automatically). Required for the PWA and
      for protecting login tokens in transit.
- [ ] *(Optional)* IP-allowlist / VPN-gate `/app` if the centre uses fixed locations.

## Student auth model (already in code)

- PINs are **hashed** (`pbkdf2:sha256`); legacy plaintext PINs upgrade to a hash on
  next login. PINs are short numeric by design (just to separate kids on shared
  laptops) — login has a per-student **lockout** (8 wrong tries → 5-min cooldown).
- Each student gets a per-student **token** at login/signup; `submit_attempt` and
  `get_progress` require it, so a guest can't forge attempts or read another child's
  progress by guessing an id.
- Login is **by name + PIN** (`login_by_name`) — the roster is never listed to the
  public, and errors are generic (no "does this name exist?" enumeration).
- Self-signups land in the **"New Learners"** cohort, isolated from facilitator-managed
  centres, and require a guardian/teacher consent acknowledgement in the UI.

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
