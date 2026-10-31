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

Only sources marked `republish` in sentiment/config.py (Bank of England, Federal Reserve, HM
Treasury, ONS: official or open-licence) have their headline text published in the public JSON.
Every other source (BBC, Guardian, Sky, CNBC, ...) is scored and counted, but only our own
one-line reason is published: news sites' RSS terms are for personal use, and a public repo is a
republication.
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
import audit  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import scorer  # noqa: E402

DATA_DIR = Path(os.environ.get("SENTIMENT_DATA_DIR") or ROOT / "data")
SIGNAL_FILE = DATA_DIR / "sentiment.json"
HISTORY_FILE = DATA_DIR / "sentiment-history.json"
AUDIT_FILE = DATA_DIR / "source-audit.json"  # counts only, no headline text: which feeds repeat or contradict

HISTORY_MAX_POINTS = 24 * 60  # one point per run -> far more than the weeks of history needed
KEEP_STORIES_HOURS = 72       # stored stories older than this are deleted
# Every story the app needs to show the full list and let the owner flag any of them (v88): all scored stories in the
# 24h window -- neutral ones too -- plus directional ones from the rest of the stored window.
HEADLINES_PUBLISHED = 200
REPUBLISH_TEXT_FROM = config.republish_sources()  # an allow-list: a new feed is withheld until marked republish


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
    # A feed dropped from config.FEEDS (owner decision: audit first, then drop whole sites) must also vanish from the stored window, or
    # its stories would keep counting for up to KEEP_STORIES_HOURS. Derived stories ("surprise" data-release events) have no feed, so
    # only rows from a named news source that is no longer configured are removed.
    configured = [f["name"] for f in config.FEEDS]
    dropped = conn.execute(
        "SELECT source, count(*) n FROM articles WHERE scorer IS NOT 'surprise' AND source NOT IN (%s) GROUP BY source"
        % ",".join("?" * len(configured)), configured).fetchall()
    for row in dropped:
        print(f"Purging {row['n']} stored story(ies) from {row['source']}: no longer a configured feed.")
    if dropped:
        conn.execute("DELETE FROM articles WHERE scorer IS NOT 'surprise' AND source NOT IN (%s)" % ",".join("?" * len(configured)),
                     configured)
        conn.commit()
    conn.commit()

    # Only stories inside the longest published window can affect the output; each is scored once
    # and then remembered, so the Claude scorer (if enabled) never re-pays for a story it has judged.
    window_start = now - max(config.WINDOWS_HOURS) * 3600
    scored_fresh = 0
    for row in db.unscored(conn, limit=500):
        if row["published_ts"] < window_start:
            continue
        res, name = scorer.score(row["title"], region=config.region_for(row["source"]))
        db.save_score(conn, row["id"], res["direction"], res["confidence"],
                      res["driver"], res["reason"], name)
        scored_fresh += 1

    signal = aggregate.build_signal(conn, now=now, headline_limit=200)
    # Stories (same-story duplicates already merged by aggregate.dedupe) that carry a direction,
    # newest first. A story from a non-whitelisted source is published with OUR OWN one-line reason and
    # its score, but without its headline text or link (see the module docstring), so the app can still
    # show why it counted without republishing a news site's content.
    feed_weight = {f["name"]: f.get("weight", config.DEFAULT_SOURCE_WEIGHT) for f in config.FEEDS}
    published = []
    for h in signal["headlines"]:
        age_h = (now - h["published_ts"]) / 3600.0
        in_24h = age_h <= 24
        if not h["direction"] and not in_24h:
            continue
        # base_w = source weight x time decay at publish time. The 24h score is sum(direction * confidence * base_w)
        # / sum(confidence * base_w) over the in_24h stories, so the app can recompute it exactly after the owner
        # flags a story (aggregate.window_score).
        base_w = feed_weight.get(h["source"], config.DEFAULT_SOURCE_WEIGHT) * aggregate.decay(age_h) if in_24h else 0.0
        withheld = h["source"] not in REPUBLISH_TEXT_FROM
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
            "in_24h": in_24h,
            "base_w": round(base_w, 6),
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
        "headlines_note": "Same-story duplicates are merged and counted once. Each headline is shown with its source, "
                          "link and our own reason; headline text belongs to its publisher.",
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

    # Source audit (report only; changes no score and removes no feed). A failure here must never lose the signal above.
    try:
        raw = [dict(r) for r in db.scored_since(conn, now - KEEP_STORIES_HOURS * 3600)]
        result = audit.source_audit(raw, now=now, window_hours=KEEP_STORIES_HOURS)
        AUDIT_FILE.write_text(json.dumps(result, indent=2) + "\n")
        print("Source audit (last %dh):\n%s" % (KEEP_STORIES_HOURS, audit.format_table(result)))
    except Exception as exc:  # noqa: BLE001
        print(f"Source audit skipped: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
