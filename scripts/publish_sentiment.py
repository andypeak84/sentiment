#!/usr/bin/env python3
"""
Daily GBP/USD news-sentiment publisher -- runs the gbpusd-sentiment pipeline (ingest ->
score -> same-story merge -> aggregate) inside GitHub Actions and writes the result as plain
JSON the Android app reads, as plain files in data/:

  data/sentiment.json          latest 1h/6h/24h scores + the stories behind them (the app reads this)
  data/sentiment-history.json  one small point per run -- the record that makes it possible to
                               later test whether the score actually relates to subsequent
                               GBP/USD moves (the pipeline has never been backtested; see
                               gbpusd-sentiment/MECHANICS.md section 12)

and keeps one more file that is NOT committed:

  state/sentiment.db           the stories seen and scored so far (72h of them), restored and saved
                               between runs by a private Actions cache. This is what lets a story
                               stay in the 24h window after the news feeds have dropped it, and means
                               each story is scored once (matters if the Claude scorer is enabled).

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
# The stories seen so far live in a SQLite file under state/, which the workflow persists between
# runs with a private Actions cache (NOT committed -- it holds BBC headline text). Without this
# memory every run starts empty and only sees what is in the feeds at that moment, and BBC's
# feeds rotate out roughly everything older than a day: a story from yesterday evening was gone
# by the next midday run, so a "24h" reading was really more like 12h and lost stories.
os.environ.setdefault("GBPUSD_DB", str(ROOT / "state" / "sentiment.db"))

import aggregate  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import scorer  # noqa: E402

DATA_DIR = Path(os.environ.get("SENTIMENT_DATA_DIR") or ROOT / "data")
SIGNAL_FILE = DATA_DIR / "sentiment.json"
HISTORY_FILE = DATA_DIR / "sentiment-history.json"

HISTORY_MAX_POINTS = 24 * 60  # one point per run -> far more than the weeks of history needed
KEEP_STORIES_HOURS = 72       # stored stories older than this are deleted
HEADLINES_PUBLISHED = 10
NOT_REPUBLISHED_SOURCE_PREFIXES = ("BBC",)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def scorer_signature() -> str:
    """Changes whenever the scoring logic or its keyword config changes, so stored scores
    (and relevance flags) can never silently outlive a scorer edit."""
    h = hashlib.sha1()
    for name in ("scorer.py", "config.py"):
        h.update((ROOT / "sentiment" / name).read_bytes())
    return h.hexdigest()[:12]


def main() -> None:
    now = time.time()
    if config.DB_PATH != ":memory:":
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    remembered = conn.execute("SELECT count(*) FROM articles").fetchone()[0]
    new, errors = ingest.ingest_feeds(conn)
    total_feeds = len(config.FEEDS)
    print(f"Remembered {remembered} stored article(s) from earlier runs. "
          f"Ingested {new} new from {total_feeds - len(errors)}/{total_feeds} feed(s). Errors: {errors or 'none'}")
    if len(errors) >= total_feeds:
        # Never overwrite a good previous signal with an empty one just because the
        # network was down -- the app shows the file's age and ignores it once stale.
        print("ERROR: every feed failed; leaving the previous signal in place.", file=sys.stderr)
        sys.exit(1)

    # The scorer or its keyword list changed since the stored stories were scored: re-judge them
    # all (relevance flags included -- they were set at ingest with the old keyword list).
    sig_id = scorer_signature()
    stored_sig = conn.execute("SELECT v FROM meta WHERE k='scorer_sig'").fetchone()
    if stored_sig is None or stored_sig[0] != sig_id:
        print(f"Scorer/config changed (stored {stored_sig[0] if stored_sig else None} -> {sig_id}); "
              f"re-judging every stored story.")
        for r in conn.execute("SELECT id, title FROM articles").fetchall():
            conn.execute("UPDATE articles SET scored=0, relevant=? WHERE id=?",
                         (int(ingest.is_relevant(r["title"])), r["id"]))
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('scorer_sig', ?)", (sig_id,))
        conn.commit()

    conn.execute("DELETE FROM articles WHERE published_ts < ?", (now - KEEP_STORIES_HOURS * 3600,))
    conn.commit()

    # Only stories inside the longest published window can affect the output; each is scored once
    # and then remembered, so the Claude scorer (if enabled) never re-pays for a story it has judged.
    window_start = now - max(config.WINDOWS_HOURS) * 3600
    scored_fresh = 0
    for row in db.unscored(conn, limit=500):
        if row["published_ts"] < window_start:
            continue
        res, name = scorer.score(row["title"])
        db.save_score(conn, row["id"], res["direction"], res["confidence"],
                      res["driver"], res["reason"], name)
        scored_fresh += 1

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

    print(f"Scored {scored_fresh} new headline(s). Windows: "
          + ", ".join(f"{k}={v['score']:+.3f} ({v['articles']} article(s))" for k, v in w.items()))


if __name__ == "__main__":
    main()
