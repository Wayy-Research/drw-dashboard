"""Kalshi public market-data client (no auth needed for reads).

Docs: https://docs.kalshi.com  -- base https://api.elections.kalshi.com/trade-api/v2
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.error
from typing import Any

from .fetch import get_json

log = logging.getLogger("kalshi_midterms.kalshi")

BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")



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


# Per-race series on Kalshi: HOUSEPA10, SENATEGA, GOVPARTYAZ (plus KX-prefixed variants).
RACE_SERIES_RE = re.compile(r"^(?:KX)?(HOUSE|SENATE|GOVPARTY|GOV)([A-Z]{2})(\d{1,2}|AL)?$")
REQUEST_GAP = float(os.environ.get("KALSHI_REQUEST_GAP", "0.25"))  # seconds between calls
SERIES_TTL = 3600.0
_series_cache: dict[str, Any] = {"at": 0.0, "tickers": []}


def _get(path: str, params: dict[str, Any]) -> dict:
    """GET with backoff on 429 (Kalshi rate-limits per IP)."""
    delay = 2.0
    for attempt in range(5):
        try:
            return get_json(f"{BASE}{path}", params)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def _paginate(path: str, params: dict[str, Any], key: str, max_pages: int = 25) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        data = _get(path, {**params, "cursor": cursor})
        out.extend(data.get(key) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(REQUEST_GAP)
    return out


def discover_series() -> list[str]:
    """Race series tickers, cached for an hour. KALSHI_SERIES (comma list) overrides."""
    override = os.environ.get("KALSHI_SERIES", "").strip()
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()]
    if _series_cache["tickers"] and time.time() - _series_cache["at"] < SERIES_TTL:
        return _series_cache["tickers"]
    tickers: set[str] = set()
    for cat in ("Elections", "Politics"):
        for s in _get("/series", {"category": cat}).get("series") or []:
            if RACE_SERIES_RE.match(s.get("ticker", "")):
                tickers.add(s["ticker"])
        time.sleep(REQUEST_GAP)
    _series_cache.update(at=time.time(), tickers=sorted(tickers))
    return _series_cache["tickers"]


def _markets_for_series(ticker: str) -> list[dict]:
    return _paginate("/markets", {"series_ticker": ticker, "status": "open", "limit": 1000},
                     "markets", max_pages=5)


def fetch_events() -> list[dict]:
    """Open race markets grouped into events: [{event_ticker, series_ticker, title, markets}]."""
    events: dict[str, dict] = {}
    for ticker in discover_series():
        try:
            markets = _markets_for_series(ticker)
        except urllib.error.HTTPError as exc:
            log.warning("series %s: HTTP %s", ticker, exc.code)
            markets = []
        for m in markets:
            et = m.get("event_ticker") or m.get("ticker", "")
            ev = events.setdefault(et, {"event_ticker": et, "series_ticker": ticker,
                                        "title": m.get("title", ""), "markets": []})
            ev["markets"].append(m)
        time.sleep(REQUEST_GAP)
    return list(events.values())
