#!/usr/bin/env python3
"""
Hourly GBP/USD news-sentiment publisher -- runs the gbpusd-sentiment pipeline (ingest ->
score -> aggregate) statelessly inside GitHub Actions and writes the result as plain JSON
the Android app reads, as plain files in data/:

  data/sentiment.json          latest 1h/6h/24h scores + recent headlines (the app reads this)
  data/sentiment-history.json  one small point per run, kept ~60 days -- the record that
                               makes it possible to later test whether the score actually
                               relates to subsequent GBP/USD moves (the pipeline has never
                               been backtested; see gbpusd-sentiment/MECHANICS.md section 12)
  data/sentiment-cache.json    score cache, so each headline is scored once and then reused
                               (matters if the Claude scorer is enabled: otherwise every
                               hourly run would re-pay for ~50 headlines it already scored)

The shared modules (config/db/ingest/scorer/aggregate) live in ./sentiment/, copied from
the gbpusd-sentiment project by its sync_to_sentiment_repo.py -- same pattern as that project's
firebase/sync_core.py. Edit them THERE, not here.

Scorer: the Claude scorer if ANTHROPIC_API_KEY is set (repo secret), otherwise the offline
rule scorer -- which currently rates most headlines neutral, so expect a near-zero signal
until the LLM scorer is enabled.

BBC headlines are scored and counted but NOT republished in the public JSON: BBC's RSS
terms have historically limited it to personal, non-commercial use, and a public repo is
a republication. Central-bank items (BoE, Fed) are republished.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sentiment"))
os.environ.setdefault("GBPUSD_DB", ":memory:")

import aggregate  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import scorer  # noqa: E402

DATA_DIR = Path(os.environ.get("SENTIMENT_DATA_DIR") or ROOT / "data")
SIGNAL_FILE = DATA_DIR / "sentiment.json"
HISTORY_FILE = DATA_DIR / "sentiment-history.json"
CACHE_FILE = DATA_DIR / "sentiment-cache.json"

HISTORY_MAX_POINTS = 24 * 60  # hourly runs -> ~60 days
CACHE_MAX_AGE_HOURS = 72
HEADLINES_PUBLISHED = 10
NOT_REPUBLISHED_SOURCE_PREFIXES = ("BBC",)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def scorer_signature() -> str:
    """Changes whenever the scoring logic or its keyword config changes, so a stale
    cache can never silently outlive a scorer edit."""
    h = hashlib.sha1()
    for name in ("scorer.py", "config.py"):
        h.update((ROOT / "sentiment" / name).read_bytes())
    return h.hexdigest()[:12]


def main() -> None:
    now = time.time()
    conn = db.connect()
    new, errors = ingest.ingest_feeds(conn)
    total_feeds = len(config.FEEDS)
    print(f"Ingested {new} new article(s) from {total_feeds - len(errors)}/{total_feeds} feed(s). "
          f"Errors: {errors or 'none'}")
    if len(errors) >= total_feeds:
        # Never overwrite a good previous signal with an empty one just because the
        # network was down -- the app shows the file's age and ignores it once stale.
        print("ERROR: every feed failed; leaving the previous signal in place.", file=sys.stderr)
        sys.exit(1)

    sig_id = scorer_signature()
    cache = load_json(CACHE_FILE, {})
    if cache.get("_sig") != sig_id:
        print(f"Scorer/config changed (cache sig {cache.get('_sig')} -> {sig_id}); rescoring from scratch.")
        cache = {"_sig": sig_id}

    # Only items inside the longest published window can affect the output, and the feeds
    # carry weeks-old items (the BoE feed alone spans a month) that would otherwise be
    # re-ingested -- and, with the LLM scorer, re-paid for -- on every stateless run.
    window_start = now - max(config.WINDOWS_HOURS) * 3600
    scored_fresh = 0
    for row in db.unscored(conn, limit=500):
        if row["published_ts"] < window_start:
            continue
        entry = cache.get(row["id"])
        if entry is None:
            res, name = scorer.score(row["title"])
            entry = {
                "direction": res["direction"], "confidence": res["confidence"],
                "driver": res["driver"], "reason": res["reason"], "scorer": name,
                "published_ts": row["published_ts"],
            }
            cache[row["id"]] = entry
            scored_fresh += 1
        db.save_score(conn, row["id"], entry["direction"], entry["confidence"],
                      entry["driver"], entry["reason"], entry["scorer"])

    cutoff = now - CACHE_MAX_AGE_HOURS * 3600
    cache = {k: v for k, v in cache.items() if k == "_sig" or v.get("published_ts", 0) >= cutoff}

    signal = aggregate.build_signal(conn, now=now, headline_limit=200)
    # Stories (same-story duplicates already merged by aggregate.dedupe) that carry a direction,
    # newest first. A BBC story is published with OUR OWN one-line reason and its score, but
    # without BBC's headline text or link (see the module docstring), so the app can still show
    # why it counted without republishing BBC content.
    published = []
    for h in signal["headlines"]:
        if not h["direction"]:
            continue
        withheld = any(h["source"].startswith(p) for p in NOT_REPUBLISHED_SOURCE_PREFIXES)
        published.append({
            "title": None if withheld else h["title"],
            "url": None if withheld else h["url"],
            "title_withheld": withheld,
            "source": h["source"],
            "also_reported_by": h.get("also_reported_by", []),
            "published_ts": h["published_ts"],
            "direction": h["direction"],
            "confidence": h["confidence"],
            "driver": h["driver"],
            "reason": h["reason"],
        })
    published = published[:HEADLINES_PUBLISHED]
    neutral_stories_24h = sum(1 for h in signal["headlines"] if not h["direction"])
    scorers_24h = {}
    for h in signal["headlines"]:
        scorers_24h[h["scorer"]] = scorers_24h.get(h["scorer"], 0) + 1

    out = {
        "pair": "GBP/USD",
        "generated_ts": int(now),
        "scale": signal["scale"],
        "windows": signal["windows"],
        "scorers_24h": scorers_24h,
        "headlines": published,
        "neutral_stories_24h": neutral_stories_24h,
        "headlines_note": "Same-story duplicates are merged and counted once. BBC stories are scored "
                          "and shown with our own reason, but BBC headline text and links are not republished.",
        "feeds": {"ok": total_feeds - len(errors), "total": total_feeds},
        "disclaimer": signal["disclaimer"],
    }

    history = load_json(HISTORY_FILE, [])
    w = signal["windows"]
    history.append({
        "ts": int(now), "s1h": w["1h"]["score"], "s6h": w["6h"]["score"],
        "s24h": w["24h"]["score"], "n24h": w["24h"]["articles"],
    })
    history = history[-HISTORY_MAX_POINTS:]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(json.dumps(out, indent=2) + "\n")
    HISTORY_FILE.write_text(json.dumps(history, separators=(",", ":")) + "\n")
    CACHE_FILE.write_text(json.dumps(cache, separators=(",", ":")) + "\n")

    print(f"Scored {scored_fresh} new headline(s). Windows: "
          + ", ".join(f"{k}={v['score']:+.3f} ({v['articles']} article(s))" for k, v in w.items()))


if __name__ == "__main__":
    main()
