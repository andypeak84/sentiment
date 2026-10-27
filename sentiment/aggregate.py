"""Time-decayed rolling sentiment, in [-1, +1] (negative = bearish GBP/USD)."""
import re
import time

import config
import db


def decay(age_hours, half_life=None):
    half_life = half_life or config.HALF_LIFE_HOURS
    return 0.5 ** (max(age_hours, 0.0) / half_life)


def window_score(rows, window_hours, now=None):
    """Weighted average direction over rows inside the window.

    Each article contributes direction * confidence * source_weight * decay.
    Dividing by the total available weight keeps the result within [-1, 1],
    so one confident article and ten confident articles are comparable.
    """
    now = now or time.time()
    num = den = 0.0
    used = 0
    for r in rows:
        age_h = (now - r["published_ts"]) / 3600.0
        if age_h > window_hours:
            continue
        w = (r["confidence"] or 0.0) * (r["source_weight"] or config.DEFAULT_SOURCE_WEIGHT) * decay(age_h)
        if w <= 0:
            continue
        num += (r["direction"] or 0) * w
        den += w
        used += 1
    return (num / den if den else 0.0), used


def label(score):
    if score >= 0.4:
        return "strongly bullish"
    if score >= 0.15:
        return "mildly bullish"
    if score <= -0.4:
        return "strongly bearish"
    if score <= -0.15:
        return "mildly bearish"
    return "neutral"


# ---- Same-story de-duplication -------------------------------------------------------------
# One story carried by several outlets (or twice by one outlet's feeds) must count ONCE: the
# BBC Business and BBC Top Stories feeds both ran "Jackdaw gas field announcement", and a gas
# approval reworded by four papers would otherwise count four times and swing the average.
# Two scored items are the same story only if they have the same non-zero direction AND the
# same driver, AND either their titles are near-identical (token Jaccard >= 0.6) or they share
# a distinctive proper noun (Jackdaw, Microsoft, Zelensky ...). Merging wrongly hides a real
# second story, which is worse than not merging, so the proper-noun test skips common names.
_STOP = set("a an and are as at be but by for from has have in into is it its of on or that the "
            "their this to was were will with after amid over says say said new more than how "
            "what why who when could would may can not no".split())
_COMMON_NAMES = set("uk us u.s. britain british england english london bank government treasury "
                    "parliament chancellor minister prime president trump biden starmer reeves "
                    "fed federal reserve america american europe european china chinese russia "
                    "russian ukraine ukrainian iran iranian israel gulf middle east north south "
                    "west monday tuesday wednesday thursday friday saturday sunday january february "
                    "march april june july august september october november december bbc reuters "
                    "ftse dow nasdaq opec budget inflation economy economic markets oil energy gas "
                    "jobs tariff tariffs interest rates sterling dollar pound".split())


def _norm(word):
    w = re.sub(r"'s$", "", word.lower().replace("’", "'").strip(".'"))
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def _tokens(title):
    out = set()
    for raw in re.findall(r"[A-Za-z0-9$%.'’]+", title or ""):
        w = _norm(raw)
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
    return out


def _proper_nouns(title):
    """Capitalised, uncommon words (names). The first word is included too -- it is always
    capitalised, so it only counts when the OTHER headline also contains it (see same_story)."""
    names = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", title or ""):
        w = _norm(raw)
        if raw[0].isupper() and len(w) >= 5 and w not in _COMMON_NAMES and w not in _STOP:
            names.add(w)
    return names


# One official inflation release (the ONS CPI/borrowing figures) is written up by every outlet in
# different words with no shared name ("Inflation and government borrowing highest for a year" vs a
# paper's "UK price rises accelerate ahead of Budget"), so neither test below links them. Within a short
# window, two same-direction inflation items are treated as the one release. Only inflation: other drivers
# (jobs, growth, energy) really do have several unrelated stories in a day (two firms cutting jobs).
_RELEASE_DRIVERS = {"inflation"}
_RELEASE_WINDOW_S = 18 * 3600


def same_story(a, b):
    """a, b: scored rows (dict-like). See the block comment above for the rule."""
    if not a["direction"] and not b["direction"]:
        # Neutral stories carry no direction to double count, but since v88 the app lists every story, so the same
        # headline from two feeds (BBC Business and BBC Top Stories ran "Chancellor to cut tariffs on some food..."
        # word for word, 2026-10-27) showed twice. Only near-identical wording merges here -- no name matching.
        ta, tb = _tokens(a["title"]), _tokens(b["title"])
        return bool(ta and tb and len(ta & tb) / len(ta | tb) >= 0.8)
    if not a["direction"] or a["direction"] != b["direction"] or (a["driver"] or "") != (b["driver"] or ""):
        return False
    if (a["driver"] in _RELEASE_DRIVERS
            and abs((a["published_ts"] or 0) - (b["published_ts"] or 0)) <= _RELEASE_WINDOW_S):
        return True
    ta, tb = _tokens(a["title"]), _tokens(b["title"])
    if ta and tb and len(ta & tb) / len(ta | tb) >= 0.6:
        return True
    # A name in one headline that also appears anywhere in the other. A headline's first word is
    # always capitalised, so a name there only counts when the other headline has it as a name.
    return bool((_proper_nouns(a["title"]) & tb) or (_proper_nouns(b["title"]) & ta))


def dedupe(rows):
    """Collapse same-story rows into one, keeping the strongest (confidence x source weight)
    as the representative; newest first overall, like the input. Neutral rows merge only when
    their wording is near-identical (see same_story)."""
    rows = [dict(r) for r in rows]
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if find(i) != find(j) and same_story(rows[i], rows[j]):
                parent[find(j)] = find(i)

    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault(find(i), []).append(r)

    def strength(r):
        return (r["confidence"] or 0.0) * (r["source_weight"] or config.DEFAULT_SOURCE_WEIGHT)

    merged = []
    for members in groups.values():
        best = max(members, key=strength)
        rep = dict(best)
        rep["also_reported_by"] = sorted({m["source"] for m in members
                                          if m is not best and m["source"] and m["source"] != best["source"]})
        rep["story_items"] = len(members)
        merged.append(rep)
    merged.sort(key=lambda r: r["published_ts"], reverse=True)
    return merged


def build_signal(conn, now=None, headline_limit=20):
    now = now or time.time()
    longest = max(config.WINDOWS_HOURS)
    rows = dedupe(db.scored_since(conn, now - longest * 3600))
    windows = {}
    for h in config.WINDOWS_HOURS:
        s, n = window_score(rows, h, now)
        windows[f"{h}h"] = {"score": round(s, 3), "label": label(s), "articles": n}
    headlines = [
        {
            "title": r["title"], "source": r["source"], "url": r["url"],
            "published_ts": r["published_ts"], "direction": r["direction"],
            "confidence": r["confidence"], "driver": r["driver"], "reason": r["reason"],
            "scorer": r["scorer"], "also_reported_by": r.get("also_reported_by", []),
        }
        for r in rows[:headline_limit]
    ]
    return {
        "pair": "GBP/USD",
        "generated_ts": now,
        "scale": "-1 = strongly bearish for GBP/USD, +1 = strongly bullish",
        "windows": windows,
        "headlines": headlines,
        "disclaimer": config.PAIR_DISCLAIMER,
    }
