"""Kalshi public market-data client (no auth needed for reads).

Docs: https://docs.kalshi.com  -- base https://api.elections.kalshi.com/trade-api/v2
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .fetch import get_json

log = logging.getLogger("kalshi_midterms.kalshi")

BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")

# Series/event titles that look like individual races (or chamber control,
# which the matcher later drops because it has no state).
RACE_RE = re.compile(r"\b(senate|house|governor|gubernatorial|congress(ional)?|district)\b", re.I)
POLITICS_CATEGORIES = {"politics", "elections"}


def price(m: dict[str, Any], field: str) -> float | None:
    """Return a price in dollars (0-1). Handles `x_dollars` strings and legacy int cents."""
    dollars = m.get(f"{field}_dollars")
    if dollars not in (None, ""):
        try:
            return float(dollars)
        except (TypeError, ValueError):
            pass
    cents = m.get(field)
    if cents is None:
        return None
    try:
        return float(cents) / 100.0
    except (TypeError, ValueError):
        return None


def _num(m: dict[str, Any], field: str) -> float:
    for key in (f"{field}_fp", field):
        v = m.get(key)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def quote(m: dict[str, Any]) -> dict[str, Any]:
    """Normalised top of book. A side with no resting order is None."""
    yes_bid, yes_ask = price(m, "yes_bid"), price(m, "yes_ask")
    no_bid, no_ask = price(m, "no_bid"), price(m, "no_ask")
    if yes_bid is not None and yes_bid <= 0:
        yes_bid = None
    if yes_ask is not None and (yes_ask <= 0 or yes_ask >= 1):
        yes_ask = None
    # A NO ask is the other side of a YES bid, and vice versa.
    if no_ask is None and yes_bid is not None:
        no_ask = round(1 - yes_bid, 4)
    if no_bid is None and yes_ask is not None:
        no_bid = round(1 - yes_ask, 4)
    if no_ask is not None and (no_ask <= 0 or no_ask >= 1):
        no_ask = None
    if no_bid is not None and no_bid <= 0:
        no_bid = None
    return {
        "yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask,
        "last": price(m, "last_price"),
        "volume": _num(m, "volume"), "volume_24h": _num(m, "volume_24h"),
        "open_interest": _num(m, "open_interest"),
    }


def _paginate(path: str, params: dict[str, Any], key: str, max_pages: int = 25) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        data = get_json(f"{BASE}{path}", {**params, "cursor": cursor})
        out.extend(data.get(key) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def discover_series() -> list[str]:
    """Election race series tickers. KALSHI_SERIES (comma list) overrides discovery."""
    override = os.environ.get("KALSHI_SERIES", "").strip()
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]
    series = get_json(f"{BASE}/series", {"category": "Elections"}).get("series") or []
    series += get_json(f"{BASE}/series", {"category": "Politics"}).get("series") or []
    tickers = {s["ticker"] for s in series
               if s.get("ticker") and RACE_RE.search(s.get("title", "") + " " + s["ticker"])}
    return sorted(tickers)


def _events_for_series(ticker: str) -> list[dict]:
    return _paginate("/events", {"series_ticker": ticker, "status": "open",
                                 "with_nested_markets": "true", "limit": 200}, "events")


def _events_by_scan() -> list[dict]:
    events = _paginate("/events", {"status": "open", "with_nested_markets": "true",
                                   "limit": 200}, "events", max_pages=40)
    return [e for e in events
            if (e.get("category") or "").lower() in POLITICS_CATEGORIES
            and RACE_RE.search(f"{e.get('title', '')} {e.get('sub_title', '')}")]


def fetch_events() -> list[dict]:
    """Open election events with their markets nested under `markets`."""
    try:
        series = discover_series()
    except Exception as exc:  # noqa: BLE001 - fall back to a full scan
        log.warning("series discovery failed (%s); scanning all open events", exc)
        series = []
    if not series:
        return _dedupe(_events_by_scan())
    events: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:  # Kalshi basic tier allows ~20 reads/s
        for batch in pool.map(_events_for_series, series):
            events.extend(batch)
    return _dedupe(events)


def _dedupe(events: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for e in events:
        seen.setdefault(e.get("event_ticker") or id(e), e)
    return list(seen.values())
