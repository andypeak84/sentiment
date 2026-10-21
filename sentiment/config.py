"""Central settings. Edit feeds, weights and half-life here."""
import os

DB_PATH = os.environ.get("GBPUSD_DB", "sentiment.db")
OUT_DIR = os.environ.get("GBPUSD_OUT", "out")

# RSS feeds to poll. Verify each URL still works before relying on it.
FEEDS = [
    {"name": "Bank of England", "url": "https://www.bankofengland.co.uk/rss/news", "weight": 1.0},
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "weight": 1.0},
    # BBC feeds: URLs not yet verified. BBC terms have historically limited RSS to personal,
    # non-commercial use: re-check them before publishing the app.
    {"name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "weight": 0.7},
    {"name": "BBC Top Stories", "url": "https://feeds.bbci.co.uk/news/rss.xml", "weight": 0.5},
    # Add other news-site feeds you have the right to use, e.g.:
    # {"name": "Example Markets", "url": "...", "weight": 0.7},
]

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
