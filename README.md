# sentiment

Daily GBP/USD news-sentiment feed for the GBP/USD Predictor Android app.

A GitHub Actions workflow (`.github/workflows/publish-sentiment.yml`) runs once a day at 06:00 UTC.
It reads the Bank of England, Federal Reserve and BBC Business RSS feeds, keeps the headlines that
matter for GBP/USD, scores each one (-1 bearish, 0 neutral, +1 bullish for GBP/USD) with a set of
hand-written rules, merges the same story reported by several feeds, and writes:

- `data/sentiment.json` - the latest 1h / 6h / 24h scores and the stories behind them (the app reads this)
- `data/sentiment-history.json` - one point per run, so the score can later be tested against real price moves
- (not committed) a private Actions cache remembers the last 72h of stories, so a story stays in the 24h window after the feeds drop it

BBC headlines are scored and counted but their text and links are not republished (BBC's RSS terms are
personal / non-commercial); the file carries our own one-line reason for each instead.

The `sentiment/` modules are copied in from the private working project; do not edit them here.

Informational only. Not financial advice.
