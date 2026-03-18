"""DRW Market Madness -- Live Trading Dashboard.

WebSocket-powered real-time dashboard. No page refreshes -- data streams
to the browser via SSE (Server-Sent Events) and updates the DOM live.

Features:
    - Price sparklines per contract (inline SVG)
    - Order book ladder modal (click any team name)
    - Private leaderboard with estimated P&L for all participants
    - Strategy panel with regime/Hurst placeholders
    - NAV time series chart (inline SVG, last 100 ticks)

Usage:
    PYTHONPATH=src python -m mania.drw.dashboard
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

log = logging.getLogger("drw_dashboard")

BASE_URL = "https://games.drw.com/api/games/trading-simulator/160"
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJyaWNrQHdheXlyZXNlYXJjaC5jb20iLCJleHAiOjE3NzYzNTg4ODZ9"
    ".p7K26e4opeDJMLYJo6saDgghGabIlqyNet5lXQwz_HE"
)
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

FAIR_VALUES: dict[str, float] = {
    "Duke": 22.30, "Michigan": 20.87, "Arizona": 17.78, "Florida": 16.93,
    "Houston": 12.96, "Iowa St": 11.46, "Purdue": 10.14, "UConn": 9.93,
    "Gonzaga": 7.13, "Illinois": 7.11, "Michigan St": 5.23, "Virginia": 5.08,
    "Arkansas": 4.19, "Nebraska": 4.01, "Texas Tech": 3.65, "St Johns": 3.54,
    "Kansas": 3.26, "Wisconsin": 3.14, "Alabama": 2.94, "Louisville": 2.93,
    "Vanderbilt": 2.81, "BYU": 2.68, "Tennessee": 2.66, "Miami FL": 2.39,
    "North Carolina": 2.21, "Saint Marys": 2.08, "UCLA": 2.04,
    "Santa Clara": 1.88, "Saint Louis": 1.87, "Iowa": 1.77, "Villanova": 1.71,
    "Kentucky": 1.70, "Georgia": 1.38, "TCU": 1.36, "Clemson": 1.35,
    "Ohio St": 1.24, "Miami OH": 1.26, "High Point": 1.10, "McNeese": 1.10,
    "Texas A&M": 1.08, "South Florida": 1.04, "VCU": 1.04, "UCF": 1.23,
    "Akron": 0.97, "Missouri": 0.87, "Northern Iowa": 0.87, "NC State": 0.64,
    "Texas": 0.55, "SMU": 0.50, "Hofstra": 0.81, "Hawaii": 0.49,
    "Penn": 0.36, "Wright St": 0.48, "Troy": 0.31, "North Dakota St": 0.18,
    "Cal Baptist": 0.71, "Tennessee St": 0.23, "Utah St": 1.48,
    "Furman": 0.05, "Kennesaw St": 0.16, "Idaho": 0.03, "Siena": 0.01,
    "Long Island": 0.02, "Howard": 0.02, "UMBC": 0.00, "Queens": 0.05,
    "Prairie View": 0.00, "Lehigh": 0.02,
}

# Per-contract strategy labels (taker-only vs market-making)
from mania.config import TAKER_ONLY as _TAKER_ONLY_SET
HURST_DATA: dict[str, dict[str, Any]] = {
    sym: {"hurst": 0.50,
           "regime": "taker-only" if sym in _TAKER_ONLY_SET else "market-making"}
    for sym in FAIR_VALUES
}

OUR_USER_ID = 531

state: dict[str, Any] = {
    "account": {}, "orderbooks": {}, "orders": {}, "fills": [],
    "trades": {}, "report": {}, "last_update": 0.0, "error": None,
}

# ---------------------------------------------------------------------------
# Read-side DB pool (lazy, for history API endpoints)
# ---------------------------------------------------------------------------
_DB_HOST = "100.96.150.42"
_DB_PORT = 5432
_DB_USER = "wayy"
_DB_NAME = "wayydb"
_read_pool: asyncpg.Pool | None = None


def _get_db_password() -> str:
    """Resolve DB password from env or config file."""
    import os
    pw = os.environ.get("WAYY_DB_PASS")
    if pw:
        return pw
    config_path = Path.home() / ".config" / "wayy" / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        pw = cfg.get("db_pass")
        if pw:
            return pw
    raise RuntimeError("No DB password found.")


async def _get_read_pool() -> asyncpg.Pool:
    """Return (and lazily create) a read-only connection pool."""
    global _read_pool
    if _read_pool is None:
        _read_pool = await asyncpg.create_pool(
            host=_DB_HOST,
            port=_DB_PORT,
            user=_DB_USER,
            password=_get_db_password(),
            database=_DB_NAME,
            min_size=1,
            max_size=5,
        )
        log.info("Read pool created (%s:%s/%s)", _DB_HOST, _DB_PORT, _DB_NAME)
    return _read_pool


app = FastAPI(title="DRW Market Madness Dashboard")

# DB logging flag — set to False to disable
DB_LOGGING = True
_db_initialized = False


async def _maybe_log_to_db() -> None:
    """Log current state to wayydb (non-blocking, errors silently)."""
    global _db_initialized
    if not DB_LOGGING:
        return
    try:
        from mania.drw.db_logger import (
            init_db, log_trades, log_orderbook_snapshot,
            log_positions, log_pnl, log_leaderboard,
        )
        if not _db_initialized:
            await init_db()
            _db_initialized = True

        account = state.get("account", {})
        books = state.get("orderbooks", {})
        trades = state.get("trades", {})
        report = state.get("report", {})
        orders = state.get("orders", {})

        cash = account.get("cash", 0.0)
        positions = account.get("positions", {})
        n_pos = sum(1 for v in positions.values() if v != 0)
        n_orders = len(orders) if isinstance(orders, dict) else 0

        # Compute NAV for logging
        total_val = 0.0
        for sym, qty in positions.items():
            if qty == 0:
                continue
            fv = FAIR_VALUES.get(sym, 0)
            book = books.get(sym, {})
            b = book.get("bids") or {}
            a = book.get("asks") or {}
            bb = max((float(p) for p in b if b[p] > 0), default=None)
            ba = min((float(p) for p in a if a[p] > 0), default=None)
            mid = (bb + ba) / 2 if bb and ba else bb or ba or fv
            total_val += mid * qty

        # Build leaderboard estimates from trade data
        lb_entries: list[dict[str, Any]] = []
        participants: dict[int, dict[str, Any]] = {}
        for sym, tlist in trades.items():
            for t in tlist:
                maker_id = t.get("maker_id", 0)
                taker_id = t.get("taker_id", 0)
                qty = t.get("quantity", 0)
                px = t.get("price", 0)
                for uid in (maker_id, taker_id):
                    if uid not in participants:
                        participants[uid] = {
                            "trades": 0, "positions": {}, "cash_flow": 0.0,
                        }
                participants[maker_id]["trades"] += 1
                participants[maker_id]["positions"][sym] = (
                    participants[maker_id]["positions"].get(sym, 0) + qty
                )
                participants[maker_id]["cash_flow"] -= px * qty
                participants[taker_id]["trades"] += 1
                participants[taker_id]["positions"][sym] = (
                    participants[taker_id]["positions"].get(sym, 0) - qty
                )
                participants[taker_id]["cash_flow"] += px * qty

        for uid, data in participants.items():
            pos_val = sum(
                FAIR_VALUES.get(s, 0) * q for s, q in data["positions"].items()
            )
            lb_entries.append({
                "user_id": uid,
                "estimated_pnl": round(data["cash_flow"] + pos_val, 2),
                "n_trades": data["trades"],
            })

        await asyncio.gather(
            log_trades(trades),
            log_orderbook_snapshot(books),
            log_positions(account, FAIR_VALUES, books),
            log_pnl(cash, cash + total_val, report.get("market_pl", 0), n_pos, n_orders),
            log_leaderboard(lb_entries),
            return_exceptions=True,
        )
    except Exception:
        pass  # Never let DB errors crash the dashboard


async def poll_api() -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                results = await asyncio.gather(
                    client.get(f"{BASE_URL}/account", headers=HEADERS),
                    client.get(f"{BASE_URL}/orderbooks", headers=HEADERS),
                    client.get(f"{BASE_URL}/orders", headers=HEADERS),
                    client.get(f"{BASE_URL}/fills", headers=HEADERS),
                    client.get(f"{BASE_URL}/trades", headers=HEADERS),
                    client.get(f"{BASE_URL}/reports", headers=HEADERS),
                )
                state["account"] = results[0].json()
                state["orderbooks"] = results[1].json()
                state["orders"] = results[2].json()
                state["fills"] = results[3].json()
                state["trades"] = results[4].json()
                rpt = results[5].json()
                state["report"] = rpt[0] if isinstance(rpt, list) and rpt else {}
                state["last_update"] = time.time()
                state["error"] = None

                # Log to wayydb in background
                asyncio.create_task(_maybe_log_to_db())
            except Exception as exc:
                state["error"] = str(exc)
            await asyncio.sleep(2)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(poll_api())


def _build_snapshot() -> dict:
    """Build a JSON snapshot of the full dashboard state."""
    account = state["account"]
    orderbooks = state["orderbooks"]
    orders = state["orders"]
    fills = state["fills"]
    trades = state["trades"]
    report = state.get("report", {})

    cash = account.get("cash", 0.0)
    positions = account.get("positions", {})

    # ── Positions with P&L ──────────────────────────────────────────
    pos_rows: list[dict] = []
    total_pos_value = 0.0
    total_pnl = 0.0
    for team, qty in positions.items():
        if qty == 0:
            continue
        fv = FAIR_VALUES.get(team, 0.0)
        book = orderbooks.get(team, {})
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}
        bb = max((float(p) for p in bids if bids[p] > 0), default=None)
        ba = min((float(p) for p in asks if asks[p] > 0), default=None)
        mid = round((bb + ba) / 2, 2) if bb and ba else bb or ba
        spread = round(ba - bb, 2) if bb and ba else None
        pnl_per = (fv - (mid or fv)) * (1 if qty > 0 else -1)
        pnl_total = pnl_per * abs(qty)
        pos_value = (mid or fv) * qty
        total_pos_value += pos_value
        total_pnl += pnl_total

        # Volume for this contract
        tlist = trades.get(team, [])
        vol = sum(abs(t["quantity"]) for t in tlist)
        notional = sum(abs(t["quantity"]) * t["price"] for t in tlist)

        # Sparkline: last 20 trade prices for this contract
        sparkline = [t["price"] for t in tlist[:20]][::-1]  # chronological

        pos_rows.append({
            "team": team, "qty": qty, "fv": fv, "mid": mid,
            "bb": bb, "ba": ba, "spread": spread,
            "pnl_per": round(pnl_per, 2), "pnl_total": round(pnl_total, 2),
            "vol": vol, "notional": round(notional, 0),
            "abs_val": abs(pos_value),
            "sparkline": sparkline,
        })
    pos_rows.sort(key=lambda r: r["abs_val"], reverse=True)

    # ── Recent trades across all contracts (last 50) ────────────────
    recent_trades: list[dict] = []
    for sym, tlist in trades.items():
        for t in tlist[:5]:
            is_ours = (
                t.get("maker_id") == OUR_USER_ID
                or t.get("taker_id") == OUR_USER_ID
            )
            recent_trades.append({
                "sym": sym, "px": t["price"], "qty": t["quantity"],
                "ts": t["timestamp"], "ours": is_ours,
            })
    recent_trades.sort(key=lambda t: -t["ts"])
    recent_trades = recent_trades[:40]

    # ── Open orders ─────────────────────────────────────────────────
    order_list: list[dict] = []
    if isinstance(orders, dict):
        for oid, o in orders.items():
            order_list.append({
                "sym": o.get("display_symbol", "?"),
                "side": o.get("side", "?").upper(),
                "px": o.get("price", 0),
                "qty": o.get("quantity", 0),
                "id": str(oid)[:8],
            })

    # ── Market overview: all 68 contracts ───────────────────────────
    market: list[dict] = []
    for sym, fv in sorted(FAIR_VALUES.items(), key=lambda x: -x[1]):
        book = orderbooks.get(sym, {})
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}
        bb = max((float(p) for p in bids if bids[p] > 0), default=None)
        ba = min((float(p) for p in asks if asks[p] > 0), default=None)
        mid = round((bb + ba) / 2, 2) if bb and ba else bb or ba
        tlist = trades.get(sym, [])
        vol = sum(abs(t["quantity"]) for t in tlist)
        pos = positions.get(sym, 0)
        edge = round(fv - (mid or fv), 2) if mid else 0
        sparkline = [t["price"] for t in tlist[:20]][::-1]
        market.append({
            "sym": sym, "fv": fv, "mid": mid, "bb": bb, "ba": ba,
            "vol": vol, "pos": pos, "edge": edge, "sparkline": sparkline,
        })

    # ── Leaderboard: estimate P&L for all participants ──────────────
    participants: dict[int, dict] = {}
    for sym, tlist in trades.items():
        for t in tlist:
            maker_id = t.get("maker_id", 0)
            taker_id = t.get("taker_id", 0)
            qty = t.get("quantity", 0)
            px = t.get("price", 0)
            for uid in (maker_id, taker_id):
                if uid not in participants:
                    participants[uid] = {
                        "trades": 0,
                        "positions": {},
                        "cash_flow": 0.0,
                    }
            # Maker buys what taker sells
            participants[maker_id]["trades"] += 1
            participants[maker_id]["positions"][sym] = (
                participants[maker_id]["positions"].get(sym, 0) + qty
            )
            participants[maker_id]["cash_flow"] -= px * qty

            participants[taker_id]["trades"] += 1
            participants[taker_id]["positions"][sym] = (
                participants[taker_id]["positions"].get(sym, 0) - qty
            )
            participants[taker_id]["cash_flow"] += px * qty

    leaderboard: list[dict] = []
    for uid, data in participants.items():
        # Mark-to-market: value positions at fair value
        pos_val = sum(
            FAIR_VALUES.get(sym, 0) * qty
            for sym, qty in data["positions"].items()
        )
        est_pnl = round(data["cash_flow"] + pos_val, 2)
        n_contracts = sum(
            1 for q in data["positions"].values() if q != 0
        )
        leaderboard.append({
            "uid": uid,
            "pnl": est_pnl,
            "trades": data["trades"],
            "n_contracts": n_contracts,
            "is_us": uid == OUR_USER_ID,
        })
    # ── Equity curves: build cumulative P&L per participant ─────
    # Collect all trades sorted by time, compute running cash + mtm
    all_trades_sorted: list[dict] = []
    for sym, tlist in trades.items():
        for t in tlist:
            all_trades_sorted.append({
                "sym": sym, "px": t["price"], "qty": t["quantity"],
                "ts": t["timestamp"],
                "maker": t.get("maker_id", 0), "taker": t.get("taker_id", 0),
            })
    all_trades_sorted.sort(key=lambda t: t["ts"])

    # Build equity curves for top participants (limit to top 15 + us)
    top_uids = set()
    # Pre-sort to find top participants
    _pre_lb = sorted(participants.items(), key=lambda x: -(x[1]["cash_flow"] + sum(FAIR_VALUES.get(s, 0) * q for s, q in x[1]["positions"].items())))
    for uid, _ in _pre_lb[:15]:
        top_uids.add(uid)
    top_uids.add(OUR_USER_ID)

    equity_curves: dict[int, list[dict]] = {uid: [] for uid in top_uids}
    running_cash: dict[int, float] = {uid: 0.0 for uid in top_uids}
    running_pos: dict[int, dict[str, int]] = {uid: {} for uid in top_uids}

    # Sample every ~50th trade to keep data manageable
    sample_interval = max(len(all_trades_sorted) // 200, 1)

    for i, t in enumerate(all_trades_sorted):
        maker, taker = t["maker"], t["taker"]
        sym, px, qty = t["sym"], t["px"], t["qty"]

        # Update maker (buys qty)
        if maker in top_uids:
            running_cash[maker] -= px * qty
            running_pos[maker][sym] = running_pos[maker].get(sym, 0) + qty
        # Update taker (sells qty)
        if taker in top_uids:
            running_cash[taker] += px * qty
            running_pos[taker][sym] = running_pos[taker].get(sym, 0) - qty

        # Sample points
        if i % sample_interval == 0:
            for uid in top_uids:
                if uid == maker or uid == taker or i % (sample_interval * 5) == 0:
                    pos_val = sum(FAIR_VALUES.get(s, 0) * q for s, q in running_pos[uid].items())
                    equity_curves[uid].append({
                        "ts": t["ts"],
                        "pnl": round(running_cash[uid] + pos_val, 2),
                    })

    # Convert to serializable format
    equity_data = []
    for uid in top_uids:
        curve = equity_curves.get(uid, [])
        if len(curve) < 2:
            continue
        equity_data.append({
            "uid": uid,
            "is_us": uid == OUR_USER_ID,
            "pnl": curve[-1]["pnl"] if curve else 0,
            "curve": [p["pnl"] for p in curve],  # Just P&L values, evenly sampled
        })
    equity_data.sort(key=lambda x: -x["pnl"])

    leaderboard.sort(key=lambda x: -x["pnl"])
    # Assign rank
    for i, entry in enumerate(leaderboard):
        entry["rank"] = i + 1

    # ── Strategy panel ──────────────────────────────────────────────
    strategy_rows: list[dict] = []
    for team, qty in positions.items():
        if qty == 0:
            continue
        h = HURST_DATA.get(team, {})
        fv = FAIR_VALUES.get(team, 0.0)
        book = orderbooks.get(team, {})
        bids_d = book.get("bids") or {}
        asks_d = book.get("asks") or {}
        bb = max((float(p) for p in bids_d if bids_d[p] > 0), default=None)
        ba = min((float(p) for p in asks_d if asks_d[p] > 0), default=None)
        mid = round((bb + ba) / 2, 2) if bb and ba else bb or ba
        strategy_rows.append({
            "team": team,
            "qty": qty,
            "strategy": "adaptive_mm",
            "hurst": h.get("hurst", 0.50),
            "regime": h.get("regime", "unknown"),
            "fv": fv,
            "mid": mid,
            "edge": round(fv - (mid or fv), 2) if mid else 0,
        })
    strategy_rows.sort(key=lambda r: abs(r["qty"]), reverse=True)

    nav = cash + total_pos_value
    return {
        "nav": round(nav, 2), "cash": round(cash, 2),
        "pnl": round(total_pnl, 2),
        "market_pl": round(report.get("market_pl", 0), 2),
        "total_orders": report.get("total_orders", 0),
        "maker_qty": report.get("total_maker_quantity", 0),
        "taker_qty": report.get("total_taker_quantity", 0),
        "n_orders": len(order_list),
        "n_positions": len(pos_rows),
        "positions": pos_rows, "orders": order_list,
        "recent_trades": recent_trades, "market": market,
        "leaderboard": leaderboard[:150],
        "equity_curves": equity_data[:20],  # Top 15 + us
        "strategy": strategy_rows,
        "ts": time.time(),
    }


@app.get("/api/snapshot")
async def api_snapshot() -> dict:
    return _build_snapshot()


@app.get("/api/book/{symbol}")
async def api_book(symbol: str) -> dict:
    """Return order book ladder for a single symbol."""
    book = state["orderbooks"].get(symbol, {})
    bids = [
        (float(p), q)
        for p, q in (book.get("bids") or {}).items()
        if q > 0
    ]
    asks = [
        (float(p), q)
        for p, q in (book.get("asks") or {}).items()
        if q > 0
    ]
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {
        "symbol": symbol,
        "bids": bids[:15],
        "asks": asks[:15],
        "fv": FAIR_VALUES.get(symbol, 0),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# History API endpoints (read from wayydb via asyncpg)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@app.get("/api/history/pnl")
async def api_history_pnl(
    hours: float = Query(24, ge=0.1, le=168, description="Lookback hours"),
    interval: str = Query("5m", regex="^(1m|5m|15m|1h)$", description="Sampling interval"),
) -> dict[str, Any]:
    """Return NAV time series for charting, downsampled to at most 500 points."""
    try:
        pool = await _get_read_pool()
    except Exception:
        return {"times": [], "nav": [], "cash": [], "market_pl": []}

    # Map interval string to a date_trunc bucket or minute divisor
    interval_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
    bucket_min = interval_minutes.get(interval, 5)

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    date_trunc('hour', timestamp)
                        + (EXTRACT(minute FROM timestamp)::int / $1) * ($1 || ' minutes')::interval
                        AS bucket,
                    AVG(nav) AS nav,
                    AVG(cash) AS cash,
                    AVG(market_pl) AS market_pl
                FROM drw_pnl_snapshots
                WHERE timestamp >= NOW() - ($2 || ' hours')::interval
                GROUP BY bucket
                ORDER BY bucket
                LIMIT 500
                """,
                bucket_min,
                str(hours),
            )
        times = [r["bucket"].isoformat() for r in rows]
        nav = [round(float(r["nav"]), 2) if r["nav"] is not None else None for r in rows]
        cash = [round(float(r["cash"]), 2) if r["cash"] is not None else None for r in rows]
        market_pl = [round(float(r["market_pl"]), 2) if r["market_pl"] is not None else None for r in rows]
        return {"times": times, "nav": nav, "cash": cash, "market_pl": market_pl}
    except Exception as exc:
        log.warning("history/pnl query failed: %s", exc)
        return {"times": [], "nav": [], "cash": [], "market_pl": []}


@app.get("/api/history/trades/{symbol}")
async def api_history_trades(
    symbol: str,
    hours: float = Query(24, ge=0.1, le=168, description="Lookback hours"),
    limit: int = Query(200, ge=1, le=1000, description="Max rows"),
) -> dict[str, Any]:
    """Trade history for one contract."""
    try:
        pool = await _get_read_pool()
    except Exception:
        return {"trades": []}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, price, quantity, maker_id, taker_id
                FROM drw_trades
                WHERE symbol = $1
                  AND timestamp >= NOW() - ($2 || ' hours')::interval
                ORDER BY timestamp DESC
                LIMIT $3
                """,
                symbol,
                str(hours),
                limit,
            )
        trades = [
            {
                "ts": r["timestamp"].isoformat(),
                "price": round(float(r["price"]), 2),
                "qty": int(r["quantity"]),
                "is_ours": r["maker_id"] == OUR_USER_ID or r["taker_id"] == OUR_USER_ID,
            }
            for r in rows
        ]
        return {"trades": trades}
    except Exception as exc:
        log.warning("history/trades query failed: %s", exc)
        return {"trades": []}


@app.get("/api/history/fills")
async def api_history_fills(
    hours: float = Query(24, ge=0.1, le=168, description="Lookback hours"),
) -> dict[str, Any]:
    """Our fill history."""
    try:
        pool = await _get_read_pool()
    except Exception:
        return {"fills": []}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, symbol, side, price, quantity, edge
                FROM drw_fills
                WHERE timestamp >= NOW() - ($1 || ' hours')::interval
                ORDER BY timestamp DESC
                """,
                str(hours),
            )
        fills = [
            {
                "ts": r["timestamp"].isoformat(),
                "symbol": r["symbol"],
                "side": r["side"],
                "price": round(float(r["price"]), 2),
                "qty": int(r["quantity"]),
                "edge": round(float(r["edge"]), 4) if r["edge"] is not None else None,
            }
            for r in rows
        ]
        return {"fills": fills}
    except Exception as exc:
        log.warning("history/fills query failed: %s", exc)
        return {"fills": []}


@app.get("/api/history/positions")
async def api_history_positions(
    hours: float = Query(24, ge=0.1, le=168, description="Lookback hours"),
    symbol: Optional[str] = Query(None, description="Filter to one contract"),
) -> dict[str, Any]:
    """Position history for charting, sampled to ~200 points."""
    try:
        pool = await _get_read_pool()
    except Exception:
        return {"snapshots": []}

    try:
        # First get total row count to compute sampling modulo
        async with pool.acquire() as conn:
            # Use ROW_NUMBER modulo sampling to get ~200 distinct timestamps
            if symbol is not None:
                rows = await conn.fetch(
                    """
                    WITH numbered AS (
                        SELECT timestamp, symbol, position,
                               ROW_NUMBER() OVER (ORDER BY timestamp) AS rn,
                               COUNT(*) OVER () AS total
                        FROM drw_positions
                        WHERE timestamp >= NOW() - ($1 || ' hours')::interval
                          AND symbol = $2
                    )
                    SELECT timestamp, symbol, position
                    FROM numbered
                    WHERE rn % GREATEST(total / 200, 1) = 0
                    ORDER BY timestamp
                    """,
                    str(hours),
                    symbol,
                )
            else:
                # Get distinct timestamp buckets, then pivot positions
                rows = await conn.fetch(
                    """
                    WITH ts_buckets AS (
                        SELECT DISTINCT timestamp
                        FROM drw_positions
                        WHERE timestamp >= NOW() - ($1 || ' hours')::interval
                        ORDER BY timestamp
                    ),
                    numbered AS (
                        SELECT timestamp,
                               ROW_NUMBER() OVER (ORDER BY timestamp) AS rn,
                               COUNT(*) OVER () AS total
                        FROM ts_buckets
                    ),
                    sampled_ts AS (
                        SELECT timestamp
                        FROM numbered
                        WHERE rn % GREATEST(total / 200, 1) = 0
                    )
                    SELECT p.timestamp, p.symbol, p.position
                    FROM drw_positions p
                    INNER JOIN sampled_ts s ON p.timestamp = s.timestamp
                    ORDER BY p.timestamp, p.symbol
                    """,
                    str(hours),
                )

        # Group by timestamp -> {symbol: qty}
        snapshots_map: dict[str, dict[str, int]] = {}
        for r in rows:
            ts_key = r["timestamp"].isoformat()
            if ts_key not in snapshots_map:
                snapshots_map[ts_key] = {}
            snapshots_map[ts_key][r["symbol"]] = int(r["position"])

        snapshots = [
            {"ts": ts, "positions": pos}
            for ts, pos in snapshots_map.items()
        ]
        return {"snapshots": snapshots}
    except Exception as exc:
        log.warning("history/positions query failed: %s", exc)
        return {"snapshots": []}


@app.get("/api/history/spreads")
async def api_history_spreads(
    symbol: str = Query(..., description="Contract symbol (required)"),
    hours: float = Query(6, ge=0.1, le=168, description="Lookback hours"),
) -> dict[str, Any]:
    """Spread history per contract."""
    try:
        pool = await _get_read_pool()
    except Exception:
        return {"data": []}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH numbered AS (
                    SELECT timestamp, best_bid, best_ask, mid, spread,
                           ROW_NUMBER() OVER (ORDER BY timestamp) AS rn,
                           COUNT(*) OVER () AS total
                    FROM drw_orderbook_snapshots
                    WHERE symbol = $1
                      AND timestamp >= NOW() - ($2 || ' hours')::interval
                )
                SELECT timestamp, best_bid, best_ask, mid, spread
                FROM numbered
                WHERE rn % GREATEST(total / 500, 1) = 0
                ORDER BY timestamp
                """,
                symbol,
                str(hours),
            )
        data = [
            {
                "ts": r["timestamp"].isoformat(),
                "bid": round(float(r["best_bid"]), 2) if r["best_bid"] is not None else None,
                "ask": round(float(r["best_ask"]), 2) if r["best_ask"] is not None else None,
                "mid": round(float(r["mid"]), 4) if r["mid"] is not None else None,
                "spread": round(float(r["spread"]), 4) if r["spread"] is not None else None,
            }
            for r in rows
        ]
        return {"data": data}
    except Exception as exc:
        log.warning("history/spreads query failed: %s", exc)
        return {"data": []}


@app.get("/stream")
async def stream() -> StreamingResponse:
    """SSE endpoint -- pushes snapshots every 2 seconds."""
    async def generate():
        while True:
            data = json.dumps(_build_snapshot())
            yield f"data: {data}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return HTML


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Inline HTML / JS / CSS -- single-file Bloomberg-style dashboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DRW Market Madness | Wayy Research</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0a0e14;--card:#111820;--border:#1b2430;--border2:#253040;--text:#c9d1d9;--text2:#8b949e;--text3:#545d68;--green:#00d4aa;--red:#ff4757;--blue:#3b82f6;--yellow:#e3b341;--font:'Inter',sans-serif;--mono:'JetBrains Mono',monospace}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}

/* Header */
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--border);background:var(--card)}
.header-left{display:flex;align-items:center;gap:12px}
.logo{font-size:1rem;font-weight:700;color:#fff;letter-spacing:-0.02em}
.logo span{color:var(--blue)}
.live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.header-right{display:flex;align-items:center;gap:16px;font-size:.75rem;color:var(--text2)}
.regime-badge{padding:3px 10px;border-radius:4px;font-size:.7rem;font-weight:600;letter-spacing:.03em}
.regime-badge.mr{background:#00d4aa18;color:var(--green);border:1px solid #00d4aa33}
.regime-badge.tr{background:#e3b34118;color:var(--yellow);border:1px solid #e3b34133}

/* KPI Cards */
.kpi-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;padding:12px 20px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 14px;backdrop-filter:blur(8px)}
.kpi .label{font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);margin-bottom:4px}
.kpi .value{font-size:1.2rem;font-weight:700;font-family:var(--mono);transition:color .3s}
.kpi .delta{font-size:.7rem;font-family:var(--mono);margin-top:2px}

/* Main grid */
.main-grid{display:grid;grid-template-columns:3fr 2fr;gap:0;padding:0 20px}
.chart-area{min-width:0}
.sidebar{min-width:0;border-left:1px solid var(--border);padding-left:16px;margin-left:16px}

/* Card containers */
.panel-card{background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:12px;overflow:hidden}
.panel-card .card-header{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--border);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);font-weight:600}

/* Tabs */
.tabs{display:flex;gap:2px;padding:12px 20px 0;border-top:1px solid var(--border);margin-top:4px}
.tab{padding:8px 18px;border-radius:6px 6px 0 0;font-size:.8rem;font-weight:500;cursor:pointer;border:1px solid transparent;background:transparent;color:var(--text3);transition:all .15s}
.tab:hover{background:var(--card);color:var(--text)}
.tab.active{background:var(--card);color:#fff;border-color:var(--border);border-bottom-color:var(--card)}
.panel{display:none;background:var(--card);border:1px solid var(--border);border-top:none;border-radius:0 0 8px 8px;margin:0 20px 20px;max-height:420px;overflow-y:auto}
.panel.active{display:block}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:.78rem}
th{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border2);color:var(--text2);font-weight:600;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0;background:var(--card);z-index:2}
td{padding:6px 12px;border-bottom:1px solid var(--border)}
tr:nth-child(even){background:#0d1219}
tr:hover{background:#151d28}
.n{text-align:right;font-family:var(--mono)}
th.n{text-align:right}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.d{color:var(--text3)}.b{color:var(--blue)}

/* Links */
.team-link{cursor:pointer;color:var(--blue);text-decoration:none;border-bottom:1px dotted rgba(59,130,246,.3);transition:all .15s}
.team-link:hover{color:#60a5fa;border-bottom-color:#60a5fa}

/* Leaderboard */
.lb-us{background:rgba(59,130,246,.08) !important;border-left:3px solid var(--blue)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.65rem;font-weight:600;letter-spacing:.03em}
.badge-us{background:rgba(59,130,246,.15);color:var(--blue);border:1px solid rgba(59,130,246,.3)}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.8);backdrop-filter:blur(4px);z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--border2);border-radius:12px;padding:24px;width:720px;max-width:95vw;max-height:90vh;overflow-y:auto;position:relative}
.modal h2{font-size:1.1rem;font-weight:700;color:#fff;margin-bottom:4px}
.modal-sub{font-size:.75rem;color:var(--text2);margin-bottom:16px}
.modal-close{position:absolute;top:12px;right:16px;background:none;border:none;color:var(--text2);font-size:1.4rem;cursor:pointer;line-height:1}
.modal-close:hover{color:#fff}
.book-ladder{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:12px}
.book-side h3{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--text2);margin-bottom:8px;text-align:center}
.book-row{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:.78rem;font-family:var(--mono)}
.book-row .price{width:60px;text-align:right}.book-row .qty{width:40px;text-align:right}
.book-bar{height:20px;border-radius:2px;min-width:2px;transition:width .3s}
.bid-bar{background:rgba(0,212,170,.15)}.ask-bar{background:rgba(255,71,87,.15)}
.book-fv{text-align:center;padding:8px 0;font-size:.8rem;color:var(--yellow);border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:8px 0;font-family:var(--mono)}
.sparkline-cell{width:90px;vertical-align:middle}

@media(max-width:900px){
  .kpi-row{grid-template-columns:repeat(3,1fr)}
  .main-grid{grid-template-columns:1fr}
  .sidebar{border-left:none;padding-left:0;margin-left:0;margin-top:12px}
}
@media(max-width:600px){
  .kpi-row{grid-template-columns:repeat(2,1fr)}
  .header{flex-direction:column;gap:8px;align-items:flex-start}
}
</style>
</head><body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="live-dot"></div>
    <div class="logo">DRW Market Madness <span>| Wayy Research</span></div>
  </div>
  <div class="header-right">
    <span id="timestamp">Connecting...</span>
    <span class="regime-badge mr" id="regime-badge"></span>
  </div>
</div>

<!-- KPI Cards -->
<div class="kpi-row" id="kpi-row">
  <div class="kpi"><div class="label">NAV</div><div class="value" id="kpi-nav">--</div><div class="delta d" id="kpi-nav-d"></div></div>
  <div class="kpi"><div class="label">Cash</div><div class="value b" id="kpi-cash">--</div><div class="delta d" id="kpi-cash-d"></div></div>
  <div class="kpi"><div class="label">Market P&L</div><div class="value" id="kpi-mpl">--</div><div class="delta d" id="kpi-mpl-d"></div></div>
  <div class="kpi"><div class="label">Rank</div><div class="value" id="kpi-rank">--</div><div class="delta d" id="kpi-rank-d"></div></div>
  <div class="kpi"><div class="label">Fills (M/T)</div><div class="value" id="kpi-fills">--</div><div class="delta d" id="kpi-fills-d"></div></div>
  <div class="kpi"><div class="label">Open Orders</div><div class="value" id="kpi-orders">--</div><div class="delta d" id="kpi-orders-d"></div></div>
</div>

<!-- Main Grid: Charts + Sidebar -->
<div class="main-grid">
  <div class="chart-area">
    <div class="panel-card">
      <div class="card-header"><span>NAV History</span><span id="nav-val" style="font-family:var(--mono);color:#fff"></span></div>
      <div id="nav-chart" style="height:260px"></div>
    </div>
    <div class="panel-card">
      <div class="card-header">Market P&L</div>
      <div id="mpl-chart" style="height:100px"></div>
    </div>
  </div>
  <div class="sidebar">
    <div class="panel-card">
      <div class="card-header"><span>Equity Curves</span><span class="b" style="font-size:.65rem">blue = us</span></div>
      <div id="eq-chart" style="height:200px"></div>
    </div>
    <div class="panel-card">
      <div class="card-header"><span>Leaderboard</span><span id="lb-rank-text" class="b" style="font-family:var(--mono);font-size:.7rem"></span></div>
      <div id="leaderboard-wrap" style="max-height:200px;overflow-y:auto"></div>
    </div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs" id="tab-bar">
  <div class="tab active" data-panel="positions">Positions</div>
  <div class="tab" data-panel="market">Market</div>
  <div class="tab" data-panel="fills">Fills</div>
  <div class="tab" data-panel="orders">Orders</div>
  <div class="tab" data-panel="strategy">Strategy</div>
</div>
<div class="panel active" id="positions"></div>
<div class="panel" id="market"></div>
<div class="panel" id="fills"></div>
<div class="panel" id="orders"></div>
<div class="panel" id="strategy"></div>

<!-- Contract Detail Modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <h2 id="modal-title">Contract Detail</h2>
    <div class="modal-sub" id="modal-sub"></div>
    <div id="modal-price-chart" style="height:200px;margin-bottom:12px"></div>
    <div id="modal-spread-chart" style="height:100px;margin-bottom:16px"></div>
    <div id="modal-book"></div>
  </div>
</div>

<script>
var $=function(id){return document.getElementById(id)};
var LC=window.LightweightCharts;
var prevSnap={};
var historyLoaded=false;
var fillsLoaded=false;

/* ── Formatters ──────────────────────────────────────────── */
function fmt(v,d){d=d||2;return(v>=0?'+':'')+v.toFixed(d)}
function fmtD(v){return'$'+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
function cc(v){return v>0.005?'g':v<-0.005?'r':'d'}
function sparkSVG(prices,w,h){
  w=w||90;h=h||24;
  if(!prices||prices.length<2) return '<svg width="'+w+'" height="'+h+'"></svg>';
  var mn=Math.min.apply(null,prices),mx=Math.max.apply(null,prices),rng=mx-mn||1,pad=2,pts=[];
  for(var i=0;i<prices.length;i++){
    var x=pad+(i/(prices.length-1))*(w-2*pad);
    var y=(h-pad)-((prices[i]-mn)/rng)*(h-2*pad);
    pts.push(x.toFixed(1)+','+y.toFixed(1));
  }
  var col=prices[prices.length-1]>=prices[0]?'var(--green)':'var(--red)';
  return '<svg width="'+w+'" height="'+h+'" style="vertical-align:middle"><polyline points="'+pts.join(' ')+'" fill="none" stroke="'+col+'" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
}

/* ── Chart theme ─────────────────────────────────────────── */
var chartOpts={
  layout:{background:{color:'#111820'},textColor:'#8b949e',fontSize:10,fontFamily:'JetBrains Mono,monospace'},
  grid:{vertLines:{color:'#1b2430'},horzLines:{color:'#1b2430'}},
  crosshair:{mode:0},
  rightPriceScale:{borderColor:'#1b2430'},
  timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#1b2430'},
};

/* ── NAV Chart ───────────────────────────────────────────── */
var navChart,navSeries,mplChart,mplSeries;
function initCharts(){
  if(navChart) return;
  var el=$('nav-chart');
  if(!el||!LC) return;
  navChart=LC.createChart(el,Object.assign({},chartOpts,{width:el.clientWidth,height:260}));
  navSeries=navChart.addAreaSeries({lineColor:'#3b82f6',topColor:'rgba(59,130,246,0.12)',bottomColor:'rgba(59,130,246,0)',lineWidth:2,priceFormat:{type:'price',precision:2,minMove:0.01}});

  var el2=$('mpl-chart');
  mplChart=LC.createChart(el2,Object.assign({},chartOpts,{width:el2.clientWidth,height:100}));
  mplSeries=mplChart.addAreaSeries({lineColor:'#00d4aa',topColor:'rgba(0,212,170,0.1)',bottomColor:'rgba(0,212,170,0)',lineWidth:1.5,priceFormat:{type:'price',precision:2,minMove:0.01}});

  window.addEventListener('resize',function(){
    navChart.applyOptions({width:$('nav-chart').clientWidth});
    mplChart.applyOptions({width:$('mpl-chart').clientWidth});
    if(eqChart) eqChart.applyOptions({width:$('eq-chart').clientWidth});
  });
}

/* ── Load PNL History ────────────────────────────────────── */
function loadHistory(){
  if(historyLoaded) return;
  historyLoaded=true;
  fetch('/api/history/pnl?hours=48&interval=5m').then(function(r){return r.json()}).then(function(d){
    if(!d.times||!d.times.length) return;
    var navPts=[],mplPts=[];
    for(var i=0;i<d.times.length;i++){
      var t=Math.floor(new Date(d.times[i]).getTime()/1000);
      if(d.nav[i]!=null) navPts.push({time:t,value:d.nav[i]});
      if(d.market_pl[i]!=null) mplPts.push({time:t,value:d.market_pl[i]});
    }
    if(navPts.length) navSeries.setData(navPts);
    if(mplPts.length) mplSeries.setData(mplPts);
    navChart.timeScale().fitContent();
    mplChart.timeScale().fitContent();
  }).catch(function(){});
}

/* ── Load Fills ──────────────────────────────────────────── */
function loadFills(){
  if(fillsLoaded) return;
  fillsLoaded=true;
  fetch('/api/history/fills?hours=48').then(function(r){return r.json()}).then(function(d){
    renderFills(d.fills||[]);
  }).catch(function(){});
}
function renderFills(fills){
  var h='<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class="n">Price</th><th class="n">Qty</th><th class="n">Edge</th></tr></thead><tbody>';
  for(var i=0;i<fills.length;i++){
    var f=fills[i];
    var sc=f.side==='BUY'?'g':'r';
    var ts=new Date(f.ts).toLocaleTimeString();
    var edge=f.edge!=null?fmt(f.edge,4):'--';
    var ec=f.edge!=null?cc(f.edge):'d';
    h+='<tr><td class="d">'+ts+'</td><td><span class="team-link" onclick="openModal(\''+f.symbol.replace(/'/g,"\\'")+ '\')">'+f.symbol+'</span></td><td class="'+sc+'">'+f.side+'</td><td class="n">'+f.price.toFixed(2)+'</td><td class="n">'+f.qty+'</td><td class="n '+ec+'">'+edge+'</td></tr>';
  }
  if(!fills.length) h+='<tr><td colspan="6" class="d" style="text-align:center;padding:20px">No fills yet</td></tr>';
  h+='</tbody></table>';
  $('fills').innerHTML=h;
}

/* ── Equity Curves ───────────────────────────────────────── */
var eqChart=null;
function buildEqChart(curves){
  var el=$('eq-chart');if(!el||!LC||!curves.length) return;
  if(eqChart){eqChart.remove();eqChart=null;}
  eqChart=LC.createChart(el,Object.assign({},chartOpts,{width:el.clientWidth,height:200,timeScale:{visible:false}}));
  var grays=['#6e7681','#545d68','#768390','#636e7b','#8b949e','#e3b341','#ff4757','#00d4aa','#d2a8ff','#ffa657'];
  var ci=0;
  for(var i=0;i<curves.length;i++){
    var c=curves[i];
    if(c.uid===634||c.curve.length<2) continue;
    var col=c.is_us?'#3b82f6':grays[ci%grays.length];
    var w=c.is_us?3:1;
    var s=eqChart.addLineSeries({color:col,lineWidth:w,priceLineVisible:false,lastValueVisible:c.is_us,crosshairMarkerVisible:false});
    var pts=[];for(var j=0;j<c.curve.length;j++) pts.push({time:j,value:c.curve[j]});
    s.setData(pts);ci++;
  }
  eqChart.timeScale().fitContent();
}

/* ── Tab switching ───────────────────────────────────────── */
$('tab-bar').addEventListener('click',function(e){
  var tab=e.target.closest('.tab');if(!tab) return;
  var p=tab.dataset.panel;
  document.querySelectorAll('.panel').forEach(function(x){x.classList.remove('active')});
  document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active')});
  $(p).classList.add('active');tab.classList.add('active');
  if(p==='fills') loadFills();
});

/* ── Contract Detail Modal ───────────────────────────────── */
function openModal(symbol){
  $('modal-overlay').classList.add('show');
  $('modal-title').textContent=symbol;
  $('modal-sub').textContent='Loading...';
  $('modal-price-chart').innerHTML='';
  $('modal-spread-chart').innerHTML='';
  $('modal-book').innerHTML='';
  Promise.all([
    fetch('/api/book/'+encodeURIComponent(symbol)).then(function(r){return r.json()}),
    fetch('/api/history/trades/'+encodeURIComponent(symbol)+'?hours=24&limit=200').then(function(r){return r.json()}).catch(function(){return{trades:[]}}),
    fetch('/api/history/spreads?hours=6&symbol='+encodeURIComponent(symbol)).then(function(r){return r.json()}).catch(function(){return{data:[]}})
  ]).then(function(results){
    var book=results[0],trades=results[1],spreads=results[2];
    $('modal-sub').textContent='FV: $'+book.fv.toFixed(2)+' | '+book.bids.length+' bids, '+book.asks.length+' asks';
    // Price chart
    if(trades.trades&&trades.trades.length>1){
      var el=$('modal-price-chart');
      var ch=LC.createChart(el,Object.assign({},chartOpts,{width:el.clientWidth,height:200}));
      var ls=ch.addLineSeries({color:'#3b82f6',lineWidth:2,priceLineVisible:false});
      var fvLine=ch.addLineSeries({color:'#e3b341',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:true});
      var pts=[];
      var t=trades.trades.slice().reverse();
      for(var i=0;i<t.length;i++){
        var ts=Math.floor(new Date(t[i].ts).getTime()/1000);
        pts.push({time:ts,value:t[i].price});
      }
      ls.setData(pts);
      if(pts.length>=2) fvLine.setData([{time:pts[0].time,value:book.fv},{time:pts[pts.length-1].time,value:book.fv}]);
      ch.timeScale().fitContent();
    }
    // Spread chart
    if(spreads.data&&spreads.data.length>1){
      var el2=$('modal-spread-chart');
      var ch2=LC.createChart(el2,Object.assign({},chartOpts,{width:el2.clientWidth,height:100}));
      var ss=ch2.addAreaSeries({lineColor:'#e3b341',topColor:'rgba(227,179,65,0.1)',bottomColor:'rgba(227,179,65,0)',lineWidth:1.5});
      var spts=[];
      for(var i=0;i<spreads.data.length;i++){
        var sd=spreads.data[i];
        if(sd.spread!=null) spts.push({time:Math.floor(new Date(sd.ts).getTime()/1000),value:sd.spread});
      }
      ss.setData(spts);ch2.timeScale().fitContent();
    }
    // Order book
    renderBook(book);
  }).catch(function(err){$('modal-sub').textContent='Error: '+err});
}
function renderBook(d){
  var maxQ=1;
  d.bids.forEach(function(b){if(b[1]>maxQ)maxQ=b[1]});
  d.asks.forEach(function(a){if(a[1]>maxQ)maxQ=a[1]});
  var h='<div class="book-fv">Fair Value: $'+d.fv.toFixed(2)+'</div><div class="book-ladder"><div class="book-side"><h3>Bids</h3>';
  for(var i=0;i<d.bids.length;i++){
    var b=d.bids[i],pct=Math.round(b[1]/maxQ*100);
    h+='<div class="book-row"><span class="price g">'+b[0].toFixed(2)+'</span><span class="qty">'+b[1]+'</span><div class="book-bar bid-bar" style="width:'+pct+'%"></div></div>';
  }
  if(!d.bids.length) h+='<div class="d" style="text-align:center;padding:12px">No bids</div>';
  h+='</div><div class="book-side"><h3>Asks</h3>';
  for(var i=0;i<d.asks.length;i++){
    var a=d.asks[i],pct=Math.round(a[1]/maxQ*100);
    h+='<div class="book-row"><span class="price r">'+a[0].toFixed(2)+'</span><span class="qty">'+a[1]+'</span><div class="book-bar ask-bar" style="width:'+pct+'%"></div></div>';
  }
  if(!d.asks.length) h+='<div class="d" style="text-align:center;padding:12px">No asks</div>';
  h+='</div></div>';
  $('modal-book').innerHTML=h;
}
function closeModal(e){
  if(e&&e.target!==$('modal-overlay')) return;
  $('modal-overlay').classList.remove('show');
}
$('modal-overlay').addEventListener('click',closeModal);
document.addEventListener('keydown',function(e){if(e.key==='Escape')$('modal-overlay').classList.remove('show')});

/* ── KPI updater with deltas ─────────────────────────────── */
function updateKPI(id,val,prev,formatter){
  var el=$(id);el.textContent=formatter?formatter(val):val;
  var dEl=$(id+'-d');if(!dEl||prev==null) return;
  var diff=val-prev;
  if(Math.abs(diff)<0.001){dEl.textContent='';return}
  dEl.textContent=(diff>=0?'+':'')+diff.toFixed(2);
  dEl.className='delta '+(diff>=0?'g':'r');
}

/* ── SSE Stream ──────────────────────────────────────────── */
var src=new EventSource('/stream');
src.onmessage=function(e){
  var d=JSON.parse(e.data);
  var now=new Date();
  $('timestamp').textContent='LIVE '+now.toLocaleTimeString();

  initCharts();
  if(!historyLoaded) loadHistory();

  // Append to NAV chart
  var ts=Math.floor(d.ts||now.getTime()/1000);
  if(navSeries) navSeries.update({time:ts,value:d.nav});
  if(mplSeries) mplSeries.update({time:ts,value:d.market_pl});
  $('nav-val').textContent=fmtD(d.nav);

  // KPIs
  updateKPI('kpi-nav',d.nav,prevSnap.nav,fmtD);
  updateKPI('kpi-cash',d.cash,prevSnap.cash,fmtD);
  $('kpi-mpl').textContent=fmt(d.market_pl);
  $('kpi-mpl').className='value '+cc(d.market_pl);
  var ourRank=null;
  var lb=d.leaderboard||[];
  for(var i=0;i<lb.length;i++){if(lb[i].is_us){ourRank=lb[i].rank;break}}
  if(ourRank){$('kpi-rank').textContent='#'+ourRank+' / '+lb.length;$('kpi-rank').className='value b'}
  $('kpi-fills').textContent=d.maker_qty.toLocaleString()+' / '+d.taker_qty.toLocaleString();
  $('kpi-orders').textContent=d.n_orders;

  // Regime badge
  var strats=d.strategy||[];
  if(strats.length){
    var mrCount=0,trCount=0;
    for(var i=0;i<strats.length;i++){if(strats[i].regime==='mean-revert')mrCount++;else if(strats[i].regime==='trending')trCount++}
    var regime=mrCount>=trCount?'Mean Revert':'Trending';
    var rb=$('regime-badge');rb.textContent=regime;rb.className='regime-badge '+(mrCount>=trCount?'mr':'tr');
  }

  // Equity curves
  buildEqChart(d.equity_curves||[]);

  // Leaderboard
  var lh='<table><thead><tr><th class="n" style="width:36px">#</th><th>User</th><th class="n">Est. P&L</th><th class="n">Trades</th></tr></thead><tbody>';
  for(var i=0;i<lb.length;i++){
    var p=lb[i],cls=p.is_us?'lb-us':'',pc=cc(p.pnl);
    var medal=p.rank<=3?['','#ffd700','#c0c0c0','#cd7f32'][p.rank]:'';
    var rk=medal?'<span style="color:'+medal+'">#'+p.rank+'</span>':'#'+p.rank;
    var tag=p.is_us?' <span class="badge badge-us">US</span>':'';
    lh+='<tr class="'+cls+'"><td class="n" style="font-weight:700">'+rk+'</td><td>'+p.uid+tag+'</td><td class="n '+pc+'">'+fmt(p.pnl)+'</td><td class="n d">'+p.trades.toLocaleString()+'</td></tr>';
  }
  if(!lb.length) lh+='<tr><td colspan="4" class="d" style="text-align:center">No data</td></tr>';
  lh+='</tbody></table>';
  $('leaderboard-wrap').innerHTML=lh;
  if(ourRank) $('lb-rank-text').textContent='#'+ourRank+' of '+lb.length;

  // Positions
  var ph='<table><thead><tr><th>Team</th><th class="n">Pos</th><th class="n">FV</th><th class="n">Bid</th><th class="n">Ask</th><th class="n">Mid</th><th class="n">Edge</th><th class="n">P&L/ct</th><th class="n">Total P&L</th><th class="n">Vol</th><th>Trend</th></tr></thead><tbody>';
  for(var i=0;i<d.positions.length;i++){
    var r=d.positions[i],pc=cc(r.pnl_total);
    var bb=r.bb!=null?r.bb.toFixed(2):'--',ba=r.ba!=null?r.ba.toFixed(2):'--';
    var mid=r.mid!=null?r.mid.toFixed(2):'--';
    var edge=r.mid!=null?(r.fv-r.mid).toFixed(2):'--';
    var ec=r.mid!=null?cc(r.fv-r.mid):'d';
    ph+='<tr><td><span class="team-link" onclick="openModal(\''+r.team.replace(/'/g,"\\'")+ '\')">'+r.team+'</span></td><td class="n">'+(r.qty>0?'+':'')+r.qty+'</td><td class="n">'+r.fv.toFixed(2)+'</td><td class="n g">'+bb+'</td><td class="n r">'+ba+'</td><td class="n">'+mid+'</td><td class="n '+ec+'">'+edge+'</td><td class="n '+pc+'">'+fmt(r.pnl_per)+'</td><td class="n '+pc+'">'+fmt(r.pnl_total)+'</td><td class="n d">'+r.vol.toLocaleString()+'</td><td class="sparkline-cell">'+sparkSVG(r.sparkline)+'</td></tr>';
  }
  if(!d.positions.length) ph+='<tr><td colspan="11" class="d" style="text-align:center;padding:20px">No positions</td></tr>';
  ph+='</tbody></table>';
  $('positions').innerHTML=ph;

  // Market
  var mh='<table><thead><tr><th>Team</th><th class="n">FV</th><th class="n">Bid</th><th class="n">Ask</th><th class="n">Mid</th><th class="n">Edge</th><th class="n">Pos</th><th class="n">Vol</th><th>Trend</th></tr></thead><tbody>';
  for(var i=0;i<d.market.length;i++){
    var m=d.market[i];
    var bb=m.bb!=null?m.bb.toFixed(2):'--',ba=m.ba!=null?m.ba.toFixed(2):'--';
    var mid=m.mid!=null?m.mid.toFixed(2):'--',ec=cc(m.edge);
    var pc=m.pos!==0?(m.pos>0?'g':'r'):'d';
    mh+='<tr><td><span class="team-link" onclick="openModal(\''+m.sym.replace(/'/g,"\\'")+ '\')">'+m.sym+'</span></td><td class="n">'+m.fv.toFixed(2)+'</td><td class="n g">'+bb+'</td><td class="n r">'+ba+'</td><td class="n">'+mid+'</td><td class="n '+ec+'">'+fmt(m.edge)+'</td><td class="n '+pc+'">'+(m.pos!==0?((m.pos>0?'+':'')+m.pos):'-')+'</td><td class="n d">'+m.vol.toLocaleString()+'</td><td class="sparkline-cell">'+sparkSVG(m.sparkline)+'</td></tr>';
  }
  mh+='</tbody></table>';
  $('market').innerHTML=mh;

  // Orders
  var oh='<table><thead><tr><th>Symbol</th><th>Side</th><th class="n">Price</th><th class="n">Qty</th><th>ID</th></tr></thead><tbody>';
  for(var i=0;i<d.orders.length;i++){
    var o=d.orders[i],sc=o.side==='BID'?'g':'r';
    oh+='<tr><td>'+o.sym+'</td><td class="'+sc+'">'+o.side+'</td><td class="n">'+o.px.toFixed(2)+'</td><td class="n">'+o.qty+'</td><td class="d" style="font-size:.65rem">'+o.id+'</td></tr>';
  }
  if(!d.orders.length) oh+='<tr><td colspan="5" class="d" style="text-align:center;padding:20px">No open orders</td></tr>';
  oh+='</tbody></table>';
  $('orders').innerHTML=oh;

  // Strategy
  var st=d.strategy||[];
  var sh='<table><thead><tr><th>Team</th><th class="n">Pos</th><th>Strategy</th><th class="n">Hurst</th><th>Regime</th><th class="n">FV</th><th class="n">Mid</th><th class="n">Edge</th></tr></thead><tbody>';
  for(var i=0;i<st.length;i++){
    var s=st[i];
    var rb=s.regime==='mean-revert'?'mr':s.regime==='trending'?'tr':'';
    var hc=s.hurst<0.45?'g':s.hurst>0.55?'y':'d';
    var mid=s.mid!=null?s.mid.toFixed(2):'--',ec=cc(s.edge);
    sh+='<tr><td><span class="team-link" onclick="openModal(\''+s.team.replace(/'/g,"\\'")+ '\')">'+s.team+'</span></td><td class="n">'+(s.qty>0?'+':'')+s.qty+'</td><td><span class="badge badge-us">adaptive_mm</span></td><td class="n '+hc+'">'+s.hurst.toFixed(2)+'</td><td><span class="regime-badge '+rb+'">'+s.regime+'</span></td><td class="n">'+s.fv.toFixed(2)+'</td><td class="n">'+mid+'</td><td class="n '+ec+'">'+fmt(s.edge)+'</td></tr>';
  }
  if(!st.length) sh+='<tr><td colspan="8" class="d" style="text-align:center;padding:20px">No active positions</td></tr>';
  sh+='</tbody></table>';
  $('strategy').innerHTML=sh;

  prevSnap=d;
};
src.onerror=function(){$('timestamp').textContent='Reconnecting...'};
</script>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
