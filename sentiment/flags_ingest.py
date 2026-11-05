"""
Reads the readers' news flags that the Android app's "Share my flags" button sends through a Google Form, and
turns them into an anonymous summary the owner can review. The daily Action pulls the form's published responses
CSV (a plain link, no key or token) -- GitHub fetches the flags, the app never writes to GitHub.

What a form response holds (no name, email or headline text -- a news site's headline is its publisher's content and
must not land in a public repo):
    Timestamp   added by Google Forms
    story       short id of the story (hex), so repeat flags of one story can be capped
    source      the news source's name, e.g. "BBC Business"
    driver      the scorer's driver for the story, e.g. "uk_rates" (what kind of story it is)
    app         what the app scored it: bullish / neutral / bearish
    mine        what the reader says: bullish / neutral / bearish

The raw rows are never published. The summary keeps only counts per source and per driver, and only for groups that
enough different stories support, so one person (or one bot) cannot move anything: each story counts at most
MAX_PER_STORY times and a group needs MIN_STORIES stories. Nothing here changes a score -- the owner reads the summary
and turns agreed patterns into scorer rules, as with the owner's own ratings (RATING_GUIDE.md).
"""
import csv
import io
import re
import urllib.request

MAX_PER_STORY = 3
MIN_STORIES = 3
MIN_ROWS = 5
RATINGS = {"bearish": -1, "neutral": 0, "bullish": 1}
_STORY_ID = re.compile(r"^[0-9a-f]{6,16}$")


def parse_rows(csv_text: str) -> list[dict]:
    """Valid responses only: a well-formed story id, both ratings recognised. Column names are matched
    case-insensitively; anything unreadable is skipped, not guessed."""
    rows = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        story = r.get("story", "").lower()
        app, mine = RATINGS.get(r.get("app", "").lower()), RATINGS.get(r.get("mine", "").lower())
        if not _STORY_ID.match(story) or app is None or mine is None:
            continue
        rows.append({"story": story, "source": r.get("source", "")[:60] or "unknown",
                     "driver": r.get("driver", "")[:40] or "unknown", "app": app, "mine": mine})
    return rows


def _cap_per_story(rows: list[dict]) -> list[dict]:
    seen: dict[str, int] = {}
    kept = []
    for r in rows:
        seen[r["story"]] = seen.get(r["story"], 0) + 1
        if seen[r["story"]] <= MAX_PER_STORY:
            kept.append(r)
    return kept


def _group(rows: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    out = {}
    for name, rs in sorted(groups.items()):
        stories = {r["story"] for r in rs}
        if len(rs) < MIN_ROWS or len(stories) < MIN_STORIES:
            continue  # too thin to say anything
        out[name] = {
            "flags": len(rs),
            "stories": len(stories),
            "disagree_pct": round(100 * sum(1 for r in rs if r["mine"] != r["app"]) / len(rs)),
            # +1 = readers rate these stories more bullish than the app does, -1 = more bearish
            "mean_shift": round(sum(r["mine"] - r["app"] for r in rs) / len(rs), 2),
        }
    return out


def summarise(rows: list[dict], now: float) -> dict:
    rows = _cap_per_story(rows)
    return {
        "generated_ts": int(now),
        "flags": len(rows),
        "stories": len({r["story"] for r in rows}),
        "by_source": _group(rows, "source"),
        "by_driver": _group(rows, "driver"),
        "note": f"Groups need {MIN_ROWS}+ flags across {MIN_STORIES}+ stories; each story counts at most {MAX_PER_STORY} times. "
                "No headline text, names or emails are collected or published.",
    }


def fetch_csv(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "gbpusd-sentiment-flags"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the owner's own published link
        return resp.read().decode("utf-8")


def build_summary(url: str, now: float) -> dict:
    return summarise(parse_rows(fetch_csv(url)), now)
