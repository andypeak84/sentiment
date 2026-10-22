"""Central settings. Edit feeds, weights and half-life here."""
import os

DB_PATH = os.environ.get("GBPUSD_DB", "sentiment.db")
OUT_DIR = os.environ.get("GBPUSD_OUT", "out")

# RSS feeds to poll. Verify each URL still works before relying on it.
# Each feed: name, url, weight (how much the scorer trusts it), and two optional fields:
#   region:    "UK" or "US" -- a hint for a source whose headlines are about one country but rarely
#              say so ("Inflation and government borrowing highest for a year ahead of next week's
#              Budget" is a BBC Business UK story with no UK word in it). Only used when the headline
#              names neither country itself (see scorer.score_rules).
#   republish: True only where the headline TEXT may be republished in the public JSON (open-licence or
#              official sources). Every other source is still scored and counted, but only our own
#              one-line reason is published, not its headline (see the publisher).
# All feeds below were fetched live and parsed on 2026-10-22.
FEEDS = [
    {"name": "Bank of England", "url": "https://www.bankofengland.co.uk/rss/news", "weight": 1.0, "region": "UK", "republish": True},
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "weight": 1.0, "region": "US", "republish": True},
    {"name": "HM Treasury", "url": "https://www.gov.uk/government/organisations/hm-treasury.atom", "weight": 1.0, "region": "UK", "republish": True},
    {"name": "ONS", "url": "https://www.ons.gov.uk/releasecalendar?rss", "weight": 1.0, "region": "UK", "republish": True},
    # Owner decision 2026-10-22: headline text and links are republished for every source.
    {"name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "weight": 0.7, "region": "UK", "republish": True},
    {"name": "BBC Top Stories", "url": "https://feeds.bbci.co.uk/news/rss.xml", "weight": 0.5, "republish": True},
    {"name": "Guardian Business", "url": "https://www.theguardian.com/uk/business/rss", "weight": 0.7, "region": "UK", "republish": True},
    {"name": "Sky News Business", "url": "https://feeds.skynews.com/feeds/rss/business.xml", "weight": 0.7, "region": "UK", "republish": True},
    {"name": "CNBC Economy", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", "weight": 0.6, "republish": True},
    {"name": "CNBC World Markets", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "weight": 0.6, "republish": True},
    {"name": "OilPrice.com", "url": "https://oilprice.com/rss/main", "weight": 0.5, "republish": True},
    {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml", "weight": 0.4, "republish": True},
    # Tried and rejected 2026-10-22: OBR (HTTP 403), Telegraph (paywalled, 402), Independent (malformed
    # XML), Yahoo Finance (404), FXStreet (timeout), ECB press (feed works, but nothing relevant to GBP/USD).
]


def region_for(source_name):
    """The region hint configured for a source, or None."""
    for feed in FEEDS:
        if feed["name"] == source_name:
            return feed.get("region")
    return None


def republish_sources():
    """Names of the sources whose headline text may be republished."""
    return {feed["name"] for feed in FEEDS if feed.get("republish")}

# How much to trust each source when no explicit feed weight applies.
DEFAULT_SOURCE_WEIGHT = 0.5

# Articles older than this many hours contribute half as much, per half-life.
HALF_LIFE_HOURS = 6.0
WINDOWS_HOURS = [1, 6, 24]

# Only articles containing one of these (lowercase) go to the scorer.
RELEVANCE_KEYWORDS = [
    "bank of england", "boe", "bailey", "mpc", "gilt", "sterling", "pound",
    "uk inflation", "uk gdp", "uk economy", "uk wages", "uk unemployment", "ons",
    "federal reserve", "the fed", "fomc", "powell", "treasury yield", "dollar",
    "us inflation", "cpi", "payrolls", "nonfarm", "us jobs", "us economy",
    "us gdp", "retail sales", "interest rate", "rate cut", "rate hike", "gbp", "usd",
    # Headline-news terms (politics, trade, general economy) for the BBC feeds. The rule scorer
    # usually rates these neutral; the Claude scorer judges them better.
    "chancellor", "budget", "tariff", "tariffs", "inflation", "recession", "trade deal",
    # Energy and sanctions (owner's rule: higher oil/gas is negative for the pound).
    "oil", "gas", "crude", "brent", "opec", "lng", "sanctions", "energy prices", "energy bills",
    "bank rate", "mortgage rates",
    # War and US policy reversals (owner's rules).
    "ukraine", "russia", "u-turn",
    # Investment (owner's rule: loss of investment is negative for the currency).
    "pulls out", "pull out", "investment", "invests", "data centre", "data center", "job cuts",
    # Added 2026-10-21 so the owner's newer rules (ratings.csv) can fire at all: 5 of the 14
    # rated headlines were being dropped HERE, before the scorer ever saw them. FTSE (falls ->
    # pound up), UK's own gas / North Sea (bullish), job cuts blamed on tax rises (bearish), and
    # Trump / Zelensky / midterms (US weakness and the US-Ukraine rift).
    "ftse", "north sea", "jackdaw", "gasfield", "gas field", "drilling",
    "tax rise", "tax rises", "tax hike", "tax hikes", "tax increase", "tax increases",
    "trump", "zelensky", "midterm", "midterms",
]

# LLM scorer (optional). Needs ANTHROPIC_API_KEY in the environment.
LLM_MODEL = os.environ.get("GBPUSD_LLM_MODEL", "claude-haiku-5-5")
USE_LLM = bool(os.environ.get("ANTHROPIC_API_KEY"))

PAIR_DISCLAIMER = "Informational only. Not financial advice."
