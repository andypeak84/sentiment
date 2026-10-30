"""Source audit: which news feeds repeat each other or contradict themselves or the other feeds.

Reporting only. Nothing here changes a score or removes a feed; the owner reads the table and decides which
sites to drop from config.FEEDS (owner decision 2026-10-30: audit first, drop whole sites only after approval).

For every source, over the stored window, it measures:
  stories            raw stories from the source
  directional        stories that carry a direction (bullish or bearish)
  kept / absorbed    stories left after the same-story merge (aggregate.dedupe) / merged into another story
  weight_share_24h   its share of the 24h directional weight (confidence x source weight x time decay)
  self_conflicts     directional stories that contradict another story from the SAME source (same driver, opposite
                     direction, within 24h)
  cross_conflicts    directional stories (24h) whose direction is opposite to the weighted majority of the OTHER
                     sources on the same driver within 12h (only counted when other sources spoke on that driver)
  kinds              news / analysis split where the feed says (OilPrice item ids: news vs article)
and proposes, but never applies, flags when a source looks like a problem (see FLAG_* below).
"""
import time
from collections import Counter

import aggregate
import config

SELF_CONFLICT_WINDOW_H = 24
CROSS_CONFLICT_WINDOW_H = 12
MIN_FOR_RATE = 3                # below this many stories a rate says nothing
FLAG_SELF_CONFLICT_RATE = 1 / 3  # a third of its directional stories contradict one of its own
FLAG_CROSS_CONFLICT_RATE = 0.5   # half its comparable stories are against the other sources' view
FLAG_WEIGHT_SHARE = 0.40         # one site supplies this much of the directional signal


def _w(row, now):
    age_h = (now - row["published_ts"]) / 3600.0
    return ((row.get("confidence") or 0.0)
            * (row.get("source_weight") or config.DEFAULT_SOURCE_WEIGHT)
            * aggregate.decay(age_h))


def _rate(count, total):
    return round(count / total, 3) if total >= MIN_FOR_RATE else None


def source_audit(raw_rows, now=None, window_hours=72):
    """raw_rows: scored stories BEFORE the same-story merge (dicts or sqlite rows with source, title, direction,
    confidence, driver, published_ts, source_weight, and optionally kind). Returns a JSON-ready dict."""
    now = now or time.time()
    raw = [dict(r) for r in raw_rows if (now - r["published_ts"]) / 3600.0 <= window_hours]
    kept = aggregate.dedupe(raw)

    per = {}

    def stat(name):
        return per.setdefault(name, {
            "stories": 0, "directional": 0, "kept": 0, "absorbed": 0, "weight_share_24h": 0.0,
            "self_conflicts": 0, "cross_conflicts": 0, "cross_checked": 0, "kinds": {}})

    for r in raw:
        s = stat(r["source"])
        s["stories"] += 1
        if r.get("direction"):
            s["directional"] += 1
        if r.get("kind"):
            s["kinds"][r["kind"]] = s["kinds"].get(r["kind"], 0) + 1
    for r in kept:
        stat(r["source"])["kept"] += 1
    for s in per.values():
        s["absorbed"] = s["stories"] - s["kept"]

    # Share of the 24h directional weight, from the merged stories (what the score is built from).
    recent = [r for r in kept if r.get("direction") and (now - r["published_ts"]) / 3600.0 <= 24]
    total_w = sum(_w(r, now) for r in recent)
    for r in recent:
        if total_w > 0:
            stat(r["source"])["weight_share_24h"] += _w(r, now) / total_w

    # Self-conflicts: the same site saying opposite things about the same driver.
    directional = [r for r in raw if r.get("direction") and r.get("driver")]
    involved = set()
    for i, a in enumerate(directional):
        for b in directional[i + 1:]:
            if (a["source"] == b["source"] and a["driver"] == b["driver"]
                    and a["direction"] != b["direction"]
                    and abs(a["published_ts"] - b["published_ts"]) <= SELF_CONFLICT_WINDOW_H * 3600):
                involved.add(id(a))
                involved.add(id(b))
    for r in directional:
        if id(r) in involved:
            stat(r["source"])["self_conflicts"] += 1

    # Conflicts with the other sources' view on the same driver.
    for r in recent:
        if not r.get("driver"):
            continue
        others = [o for o in recent
                  if o["source"] != r["source"] and o.get("driver") == r["driver"]
                  and abs(o["published_ts"] - r["published_ts"]) <= CROSS_CONFLICT_WINDOW_H * 3600]
        if not others:
            continue
        majority = sum(o["direction"] * _w(o, now) for o in others)
        s = stat(r["source"])
        s["cross_checked"] += 1
        if majority * r["direction"] < 0:
            s["cross_conflicts"] += 1

    flags = {}
    for name, s in per.items():
        s["weight_share_24h"] = round(s["weight_share_24h"], 3)
        s["self_conflict_rate"] = _rate(s["self_conflicts"], s["directional"])
        s["cross_conflict_rate"] = _rate(s["cross_conflicts"], s["cross_checked"])
        reasons = []
        if s["self_conflict_rate"] is not None and s["self_conflict_rate"] > FLAG_SELF_CONFLICT_RATE:
            reasons.append(f"contradicts itself in {s['self_conflicts']} of {s['directional']} directional stories")
        if s["cross_conflict_rate"] is not None and s["cross_conflict_rate"] >= FLAG_CROSS_CONFLICT_RATE:
            reasons.append(f"against the other sources in {s['cross_conflicts']} of {s['cross_checked']} comparable stories")
        if s["weight_share_24h"] > FLAG_WEIGHT_SHARE:
            reasons.append(f"supplies {s['weight_share_24h']:.0%} of the 24h directional signal")
        if reasons:
            flags[name] = reasons

    return {
        "generated_ts": int(now),
        "window_hours": window_hours,
        "note": "Report only. Nothing is removed or re-weighted by this file; the owner decides which sites to drop.",
        "thresholds": {"self_conflict_rate": FLAG_SELF_CONFLICT_RATE, "cross_conflict_rate": FLAG_CROSS_CONFLICT_RATE,
                       "weight_share_24h": FLAG_WEIGHT_SHARE, "min_stories_for_a_rate": MIN_FOR_RATE},
        "sources": dict(sorted(per.items(), key=lambda kv: -kv[1]["stories"])),
        "flags": flags,
    }


def format_table(audit):
    """A plain-text table for the Action log and the terminal."""
    def pct(v):
        return "  -  " if v is None else f"{v:4.0%}"
    lines = [f"{'source':<20}{'stories':>8}{'direct.':>8}{'merged':>7}{'share24h':>9}{'self-confl':>11}{'vs others':>10}  kinds"]
    for name, s in audit["sources"].items():
        kinds = ", ".join(f"{k}:{v}" for k, v in sorted(s["kinds"].items())) or ""
        lines.append(f"{name[:19]:<20}{s['stories']:>8}{s['directional']:>8}{s['absorbed']:>7}"
                     f"{s['weight_share_24h']:>9.0%}"
                     f"{(str(s['self_conflicts']) + ' ' + pct(s['self_conflict_rate'])):>11}"
                     f"{(str(s['cross_conflicts']) + '/' + str(s['cross_checked'])):>10}  {kinds}")
    if audit["flags"]:
        lines.append("")
        lines.append("Proposed flags (nothing applied):")
        for name, reasons in audit["flags"].items():
            lines.append(f"  {name}: " + "; ".join(reasons))
    return "\n".join(lines)
