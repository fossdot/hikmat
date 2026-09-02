#!/usr/bin/env python3
# Copyright (c) 2026, FOSS United and contributors
# For license information, please see license.txt
"""Curriculum lint — schema + variety checks for hikmat/data/curriculum.json.

Run after editing curriculum content, before committing:

    python3 lint-curriculum.py            # from apps/hikmat/

HARD failures (exit 1): malformed activity data — an answer not among its choices, a sort
item pointing at a missing bucket, a branch conversation that can loop forever, a pairs
board on a lesson with too few picture words, a ladder longer than 8 steps. These are the
bugs that strand a child on a broken screen.

SOFT warnings (printed, exit 0): variety drift — a track where two neighbouring lessons
carry the identical activity mix, or a type that never appears. These make the game boring,
not broken.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CURR = os.path.join(HERE, "hikmat", "data", "curriculum.json")
DENOMS = {1, 2, 5, 10, 20, 50, 100, 200, 500}
EXTRA_TYPES = ["sounds", "rhyme", "pairs", "odd", "sort", "dictate", "translate", "branch",
               "story", "cloze", "notice", "rapid", "count", "money", "clock", "order",
               "scam", "hunt", "form"]
errs, warns = [], []
err = lambda p, m: errs.append(f"{p}: {m}")


def ladder(ls):
    """Mirror of the game's levelsFor() — how many activity steps this lesson renders."""
    n = 4 if ls.get("words") else 0                       # learn/listen/spell/phrase
    n += 1 if ls.get("dialogues") else 0                  # talk
    for k in ("read", "quiz", "code", "fix", "email", "reply"):
        n += 1 if ls.get(k) else 0
    for k in EXTRA_TYPES:
        v = ls.get(k)
        n += 1 if (v is True or (isinstance(v, list) and v) or (isinstance(v, dict) and v)) else 0
    return n - len(ls.get("skip") or [])


def need(d, k, path):
    if not isinstance(d, dict) or not d.get(k):
        err(path, f"missing {k}")
        return None
    return d[k]


def check_lesson(tk, ls):
    p0 = f"{tk}/{ls.get('key')}"
    words = ls.get("words") or []
    emojiwords = sum(1 for w in words if w.get("emoji"))
    huntable = sum(1 for w in words if w.get("en") and re.fullmatch(r"[A-Za-z]{3,7}", w["en"]))

    v = ls.get("pairs")
    if v is not None:
        if v is True:
            if emojiwords < 4: err(p0 + "/pairs", "pairs:true but <4 emoji words")
        elif isinstance(v, list):
            if not 4 <= len(v) <= 6: err(p0 + "/pairs", "authored board needs 4-6 items")
            for i, it in enumerate(v):
                for k in ("en", "hi", "emoji"): need(it, k, f"{p0}/pairs[{i}]")
        else: err(p0 + "/pairs", "must be true or a list")
    for i, r in enumerate(ls.get("odd") or []):
        items = r.get("items") or []
        if not 3 <= len(items) <= 4: err(f"{p0}/odd[{i}]", "3-4 items")
        if r.get("answer") not in [it.get("en") for it in items]: err(f"{p0}/odd[{i}]", "answer not among items")
        need(r, "teach", f"{p0}/odd[{i}]"); need(r, "teachHi", f"{p0}/odd[{i}]")
    v = ls.get("sort")
    if v:
        keys = set()
        for j, b in enumerate(v.get("buckets") or []):
            k = need(b, "key", f"{p0}/sort.buckets[{j}]"); need(b, "en", f"{p0}/sort.buckets[{j}]")
            if k: keys.add(k)
        if not 2 <= len(keys) <= 3: err(p0 + "/sort", "2-3 buckets")
        its = v.get("items") or []
        if len(its) < 4: err(p0 + "/sort", "needs ≥4 items")
        for j, it in enumerate(its):
            need(it, "en", f"{p0}/sort.items[{j}]")
            if it.get("bucket") not in keys: err(f"{p0}/sort.items[{j}]", "bucket key unknown")
    for i, r in enumerate(ls.get("sounds") or []):
        need(r, "play", f"{p0}/sounds[{i}]")
        if r.get("answer") not in [c.get("en") for c in (r.get("choices") or [])]:
            err(f"{p0}/sounds[{i}]", "answer not in choices")
    for i, r in enumerate(ls.get("rhyme") or []):
        if not (r.get("target") or {}).get("en"): err(f"{p0}/rhyme[{i}]", "target.en missing")
        if r.get("answer") not in [c.get("en") for c in (r.get("choices") or [])]:
            err(f"{p0}/rhyme[{i}]", "answer not in choices")
    for i, r in enumerate(ls.get("dictate") or []):
        en = need(r, "en", f"{p0}/dictate[{i}]")
        if en and len(en.split()) > 9: err(f"{p0}/dictate[{i}]", "sentence >9 words")
    for i, r in enumerate(ls.get("translate") or []):
        en = need(r, "en", f"{p0}/translate[{i}]"); need(r, "hi", f"{p0}/translate[{i}]")
        if en and len(en.split()) > 9: err(f"{p0}/translate[{i}]", "sentence >9 words")
    for i, sc in enumerate(ls.get("branch") or []):
        nodes = sc.get("nodes") or {}
        start = sc.get("start")
        if start not in nodes: err(f"{p0}/branch[{i}]", "start not in nodes")
        for nid, n in nodes.items():
            reps = n.get("replies") or []
            need(n, "say", f"{p0}/branch[{i}].{nid}")
            if not 2 <= len(reps) <= 3: err(f"{p0}/branch[{i}].{nid}", "2-3 replies")
            if not any((rp.get("score") or 0) >= 2 for rp in reps): err(f"{p0}/branch[{i}].{nid}", "no score-2 reply")
            for j, rp in enumerate(reps):
                if rp.get("next") and rp["next"] not in nodes: err(f"{p0}/branch[{i}].{nid}[{j}]", "next unknown")

        def walk(nid, depth, seen):
            if depth > 12 or nid in seen:
                err(f"{p0}/branch[{i}]", f"cycle / runaway path at {nid}")
                return
            for rp in (nodes.get(nid, {}).get("replies") or []):
                if rp.get("next"): walk(rp["next"], depth + 1, seen | {nid})
        if start in nodes: walk(start, 0, frozenset())
    for i, r in enumerate(ls.get("story") or []):
        need(r, "text", f"{p0}/story[{i}]"); need(r, "q", f"{p0}/story[{i}]")
        if r.get("answer") not in (r.get("choices") or []): err(f"{p0}/story[{i}]", "answer not in choices")
    for i, r in enumerate(ls.get("cloze") or []):
        t = need(r, "text", f"{p0}/cloze[{i}]")
        if t and t.count("___") != 1: err(f"{p0}/cloze[{i}]", "text needs exactly one ___")
        if r.get("answer") not in (r.get("options") or []): err(f"{p0}/cloze[{i}]", "answer not in options")
    for i, r in enumerate(ls.get("notice") or []):
        need(r, "title", f"{p0}/notice[{i}]"); need(r, "q", f"{p0}/notice[{i}]")
        if not r.get("lines"): err(f"{p0}/notice[{i}]", "needs lines")
        if r.get("answer") not in (r.get("choices") or []): err(f"{p0}/notice[{i}]", "answer not in choices")
    v = ls.get("rapid")
    if v:
        truths = sum(1 for r in v if r.get("truth") is True)
        for i, r in enumerate(v):
            need(r, "en", f"{p0}/rapid[{i}]")
            if not isinstance(r.get("truth"), bool): err(f"{p0}/rapid[{i}]", "truth must be true/false")
        if truths in (0, len(v)): err(p0 + "/rapid", "mix true AND false statements")
    for i, r in enumerate(ls.get("count") or []):
        a, b, op = r.get("a"), r.get("b"), r.get("op")
        need(r, "emoji", f"{p0}/count[{i}]")
        if not (isinstance(a, int) and 1 <= a <= 10): err(f"{p0}/count[{i}]", "a must be 1-10")
        if op and op not in "+-": err(f"{p0}/count[{i}]", "op must be + or -")
        if op and not (isinstance(b, int) and 1 <= b <= 10): err(f"{p0}/count[{i}]", "b must be 1-10 with op")
        if op == "-" and isinstance(a, int) and isinstance(b, int) and a < b: err(f"{p0}/count[{i}]", "a-b negative")
        if op == "+" and isinstance(a, int) and isinstance(b, int) and a + b > 12: err(f"{p0}/count[{i}]", "a+b > 12")
    for i, r in enumerate(ls.get("money") or []):
        if "show" in r:
            if any(x not in DENOMS for x in (r["show"] or [])): err(f"{p0}/money[{i}]", "invalid denomination")
        elif "make" in r:
            if not (isinstance(r["make"], int) and 2 <= r["make"] <= 500): err(f"{p0}/money[{i}]", "make 2-500")
            if r.get("denoms") and any(x not in DENOMS for x in r["denoms"]): err(f"{p0}/money[{i}]", "bad denoms")
        else: err(f"{p0}/money[{i}]", "needs show or make")
    for i, r in enumerate(ls.get("clock") or []):
        if r.get("h") not in range(1, 13): err(f"{p0}/clock[{i}]", "h 1-12")
        if r.get("m") not in (0, 15, 30, 45): err(f"{p0}/clock[{i}]", "m in 0/15/30/45")
    for i, r in enumerate(ls.get("order") or []):
        steps = r.get("steps") or []
        need(r, "title", f"{p0}/order[{i}]")
        if not 3 <= len(steps) <= 5: err(f"{p0}/order[{i}]", "3-5 steps")
    v = ls.get("scam")
    if v:
        for i, r in enumerate(v):
            need(r, "msg", f"{p0}/scam[{i}]"); need(r, "why", f"{p0}/scam[{i}]")
            if not isinstance(r.get("safe"), bool): err(f"{p0}/scam[{i}]", "safe must be true/false")
        if not any(r.get("safe") for r in v): err(p0 + "/scam", "include at least one SAFE message")
    v = ls.get("hunt")
    if v is not None:
        if v is True:
            if huntable < 4: err(p0 + "/hunt", "hunt:true but <4 plain 3-7 letter words")
        elif isinstance(v, list):
            if len(v) != 4 or any(not (isinstance(w, str) and re.fullmatch(r"[A-Za-z]{3,7}", w)) for w in v):
                err(p0 + "/hunt", "authored hunt = exactly 4 plain 3-7 letter words")
        else: err(p0 + "/hunt", "must be true or a list of 4 words")
    for i, r in enumerate(ls.get("form") or []):
        need(r, "title", f"{p0}/form[{i}]")
        for j, f in enumerate(r.get("fields") or []):
            need(f, "label", f"{p0}/form[{i}].fields[{j}]")
            opts = f.get("options") or []
            if sum(1 for o in opts if o.get("ok")) != 1: err(f"{p0}/form[{i}].fields[{j}]", "exactly one ok option")
    if ladder(ls) > 8:
        err(p0, f"ladder is {ladder(ls)} steps (>8) — trim types or add skip")


def main():
    data = json.load(open(CURR, encoding="utf-8"))
    grand = {}
    for c in data:
        sets = []
        for ls in c.get("lessons", []):
            check_lesson(c["key"], ls)
            got = {k for k in EXTRA_TYPES if ls.get(k)}
            sets.append(got)
            for k in got: grand[k] = grand.get(k, 0) + 1
        for i in range(1, len(sets)):
            if sets[i] and sets[i] == sets[i - 1]:
                warns.append(f"{c['key']}: lessons {i} and {i+1} carry the identical activity mix {sorted(sets[i])}")
        if c.get("published", True) and c.get("lessons") and not any(sets):
            warns.append(f"{c['key']}: no rotation extras in the whole track")
    unused = [k for k in EXTRA_TYPES if not grand.get(k)]
    if unused: warns.append("types never used anywhere: " + ", ".join(unused))
    for w in warns: print("WARN", w)
    if errs:
        print(f"\n{len(errs)} hard problem(s):")
        for e in errs: print(" -", e)
        sys.exit(1)
    n = sum(1 for c in data for ls in c.get("lessons", []) if any(ls.get(k) for k in EXTRA_TYPES))
    print(f"OK — {n} lessons carry rotation extras across {len(data)} tracks; {len(warns)} warning(s)")


if __name__ == "__main__":
    main()
