"""Fetch headlines from RSS feeds, pre-filter for relevance, store new ones.

Stores headlines, links and our own scores only, not full article text.
Respect each site's terms of service and robots.txt before adding feeds.
"""
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import config
import db

UA = {"User-Agent": "gbpusd-sentiment/0.1 (personal research tool)"}


def is_relevant(title: str, summary: str = "") -> bool:
    text = f"{title} {summary}".lower()
    return any(re.search(rf"\b{re.escape(k)}\b", text) for k in config.RELEVANCE_KEYWORDS)


def _parse_ts(value):
    if not value:
        return time.time()
    try:
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        try:  # ISO 8601 (Atom)
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            return time.time()


def parse_feed(xml_bytes):
    """Yield (title, link, summary, ts) from an RSS or Atom document."""
    root = ET.fromstring(xml_bytes)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for item in root.iter("item"):
        yield (
            (item.findtext("title") or "").strip(),
            (item.findtext("link") or "").strip(),
            (item.findtext("description") or "").strip(),
            _parse_ts(item.findtext("pubDate")),
        )
    for entry in root.findall("a:entry", ns):
        link_el = entry.find("a:link", ns)
        yield (
            (entry.findtext("a:title", default="", namespaces=ns) or "").strip(),
            link_el.get("href", "") if link_el is not None else "",
            (entry.findtext("a:summary", default="", namespaces=ns) or "").strip(),
            _parse_ts(entry.findtext("a:updated", default="", namespaces=ns)),
        )


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ingest_feeds(conn, feeds=None):
    """Poll all feeds. Returns (new_articles, errors)."""
    new, errors = 0, []
    for feed in feeds or config.FEEDS:
        try:
            for title, link, summary, ts in parse_feed(fetch(feed["url"])):
                if not title:
                    continue
                rel = is_relevant(title, summary)
                if db.insert_article(conn, feed["name"], title, link, ts, rel, feed["weight"]):
                    new += 1
        except Exception as exc:  # one bad feed must not stop the rest
            errors.append(f'{feed["name"]}: {exc}')
    return new, errors
