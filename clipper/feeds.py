"""Watch a client's podcast RSS feed for new episodes."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from .util import ClipperError

MAX_FEED_BYTES = 15 * 1024 * 1024


def fetch_feed(url: str) -> bytes:
    try:
        with requests.get(url, stream=True, timeout=(15, 60), headers={"User-Agent": "clip-service/0.1"}) as response:
            response.raise_for_status()
            data = response.raw.read(MAX_FEED_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        raise ClipperError(f"Could not read the feed ({type(exc).__name__}).") from None
    if len(data) > MAX_FEED_BYTES:
        raise ClipperError("The feed is too large to read.")
    return data


def parse_feed(data: bytes) -> list[dict]:
    """Episodes in an RSS feed that carry a media file, newest first."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ClipperError(f"The feed is not valid RSS ({exc}).") from None
    episodes = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        link = (enclosure.get("url") or "").strip() if enclosure is not None else ""
        if not link.lower().startswith(("http://", "https://")):
            continue
        published = None
        try:
            published = parsedate_to_datetime((item.findtext("pubDate") or "").strip())
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        guid = (item.findtext("guid") or link).strip()
        episodes.append(
            {
                "key": hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16],
                "title": " ".join((item.findtext("title") or "Untitled episode").split()),
                "source": link,
                "published": published,
            }
        )
    episodes.sort(key=lambda e: e["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return episodes


def recent(episodes: list[dict], days: int = 7, limit: int = 2, now: datetime | None = None) -> list[dict]:
    """Only fresh episodes, so adding a feed never back-fills a show's whole archive."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    fresh = [e for e in episodes if e["published"] and e["published"] >= cutoff]
    return fresh[:limit]
