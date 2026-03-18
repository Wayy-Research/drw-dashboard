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
import time
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

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

# Placeholder Hurst exponents and regime labels per contract
HURST_DATA: dict[str, dict[str, Any]] = {
    sym: {"hurst": round(0.45 + 0.1 * (hash(sym) % 10) / 10, 2),
           "regime": "mean-revert" if hash(sym) % 3 == 0 else
                     "trending" if hash(sym) % 3 == 1 else "random-walk"}
    for sym in FAIR_VALUES
}

OUR_USER_ID = 531

state: dict[str, Any] = {
    "account": {}, "orderbooks": {}, "orders": {}, "fills": [],
    "trades": {}, "report": {}, "last_update": 0.0, "error": None,
}

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
            log_positions, log_pnl,
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

        await asyncio.gather(
            log_trades(trades),
            log_orderbook_snapshot(books),
            log_positions(account, FAIR_VALUES, books),
            log_pnl(cash, cash + total_val, report.get("market_pl", 0), n_pos, n_orders),
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
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DRW Market Madness -- Wayy Research</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,monospace;background:#0d1117;color:#c9d1d9;padding:16px 24px;min-height:100vh}
h1{font-size:1.4rem;font-weight:600;color:#e6edf3}
.sub{font-size:.75rem;color:#484f58;margin-bottom:16px}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#00e676;margin-right:6px;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* NAV chart container */
.nav-chart-wrap{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin-bottom:16px}
.nav-chart-wrap .chart-title{font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin-bottom:6px}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px}
.card .l{font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin-bottom:2px}
.card .v{font-size:1.3rem;font-weight:700;font-family:'SF Mono','Fira Code',monospace}
.tabs{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.tab{padding:6px 16px;border-radius:6px;font-size:.8rem;cursor:pointer;border:1px solid #30363d;background:#161b22;color:#8b949e;transition:all .15s}
.tab:hover{background:#1c2128;color:#c9d1d9}
.tab.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
.panel{display:none}.panel.active{display:block}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{text-align:left;padding:6px 10px;border-bottom:2px solid #30363d;color:#8b949e;font-weight:600;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0;background:#0d1117;z-index:1}
td{padding:5px 10px;border-bottom:1px solid #21262d}
tr:hover{background:#1c2128}
.n{text-align:right;font-family:'SF Mono','Fira Code',monospace}
th.n{text-align:right}
.g{color:#00e676}.r{color:#ff5252}.y{color:#e3b341}.d{color:#484f58}.b{color:#58a6ff}
.ticker{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px;margin-bottom:16px;max-height:180px;overflow-y:auto;font-family:'SF Mono',monospace;font-size:.75rem}
.ticker-row{padding:3px 0;border-bottom:1px solid #21262d;display:flex;gap:12px}
.ticker-row .sym{width:130px;font-weight:600}
.ticker-row .ours{background:#1f6feb22;border-left:2px solid #1f6feb;padding-left:8px}
.spread-bar{display:inline-block;height:4px;border-radius:2px;vertical-align:middle}

/* Clickable team names */
.team-link{cursor:pointer;color:#58a6ff;text-decoration:none;border-bottom:1px dotted #58a6ff44}
.team-link:hover{color:#79c0ff;border-bottom-color:#79c0ff}

/* Order book modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.75);z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:520px;max-width:95vw;max-height:85vh;overflow-y:auto;position:relative}
.modal h2{font-size:1.1rem;color:#e6edf3;margin-bottom:4px}
.modal .modal-sub{font-size:.75rem;color:#8b949e;margin-bottom:16px}
.modal-close{position:absolute;top:12px;right:16px;background:none;border:none;color:#8b949e;font-size:1.2rem;cursor:pointer}
.modal-close:hover{color:#e6edf3}
.book-ladder{display:flex;gap:16px}
.book-side{flex:1}
.book-side h3{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:#8b949e;margin-bottom:8px;text-align:center}
.book-row{display:flex;align-items:center;gap:6px;padding:3px 0;font-size:.78rem;font-family:'SF Mono',monospace;position:relative}
.book-row .price{width:60px;text-align:right}
.book-row .qty{width:40px;text-align:right}
.book-bar{height:20px;border-radius:2px;min-width:2px;transition:width .3s}
.bid-bar{background:#00e67633}
.ask-bar{background:#ff525233}
.book-fv{text-align:center;padding:8px 0;font-size:.8rem;color:#e3b341;border-top:1px solid #30363d;border-bottom:1px solid #30363d;margin:8px 0}

/* Leaderboard highlight */
.lb-us{background:#1f6feb18;border-left:3px solid #1f6feb}
.lb-rank{width:40px;text-align:center;font-weight:700}

/* Strategy badges */
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:600;letter-spacing:.03em}
.badge-mm{background:#1f6feb22;color:#58a6ff;border:1px solid #1f6feb44}
.badge-mr{background:#00e67622;color:#00e676;border:1px solid #00e67644}
.badge-tr{background:#e3b34122;color:#e3b341;border:1px solid #e3b34144}
.badge-rw{background:#8b949e22;color:#8b949e;border:1px solid #8b949e44}

/* Sparkline in table */
.sparkline-cell{width:100px;vertical-align:middle}
</style>
</head><body>
<h1><span class="live-dot"></span>DRW Market Madness -- Wayy Research</h1>
<div class="sub" id="sub">Connecting...</div>

<!-- NAV Time Series Chart (lightweight-charts) -->
<div class="nav-chart-wrap">
  <div class="chart-title">NAV Time Series</div>
  <div id="nav-chart" style="height:120px"></div>
</div>

<div class="cards" id="cards"></div>

<div class="section-title" style="font-size:.8rem;color:#8b949e;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px">Live Trade Ticker</div>
<div class="ticker" id="ticker"></div>

<div class="tabs" id="tab-bar">
  <div class="tab active" data-panel="positions">Positions</div>
  <div class="tab" data-panel="market">Market (68)</div>
  <div class="tab" data-panel="orders">Orders</div>
  <div class="tab" data-panel="leaderboard">Leaderboard</div>
  <div class="tab" data-panel="strategy">Strategy</div>
</div>

<div class="panel active" id="positions"></div>
<div class="panel" id="market"></div>
<div class="panel" id="orders"></div>
<div class="panel" id="leaderboard"></div>
<div class="panel" id="strategy"></div>

<!-- Order Book Modal -->
<div class="modal-overlay" id="book-overlay" onclick="closeBook(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeBook()">&times;</button>
    <h2 id="book-title">Order Book</h2>
    <div class="modal-sub" id="book-sub"></div>
    <div id="book-content"></div>
  </div>
</div>

<script>
// ── NAV chart (lightweight-charts) ───────────────────────────
var navChart = null;
var navSeries = null;
var navTickCount = 0;

function initNavChart() {
  if (navChart) return;
  var el = document.getElementById('nav-chart');
  if (!el || !window.LightweightCharts) return;
  navChart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: 120,
    layout: { background: { color: '#161b22' }, textColor: '#8b949e', fontSize: 10, fontFamily: 'SF Mono, Fira Code, monospace' },
    grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
    timeScale: { timeVisible: true, secondsVisible: true, borderColor: '#30363d' },
    rightPriceScale: { borderColor: '#30363d' },
    crosshair: { mode: 0 },
  });
  navSeries = navChart.addAreaSeries({
    lineColor: '#1f6feb', topColor: '#1f6feb33', bottomColor: '#1f6feb05',
    lineWidth: 2, priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
  });
  window.addEventListener('resize', function() { if (navChart) navChart.applyOptions({ width: el.clientWidth }); });
}

// ── Equity curves chart ───────────────────────────────────────
var eqChart = null;
var eqSeriesMap = {};  // uid -> series

// ── Tab switching ──────────────────────────────────────────────
document.getElementById('tab-bar').addEventListener('click', function(e) {
  const tab = e.target.closest('.tab');
  if (!tab) return;
  const panelId = tab.dataset.panel;
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(panelId).classList.add('active');
  tab.classList.add('active');
});

function c(v) { return v > 0.005 ? 'g' : v < -0.005 ? 'r' : 'd'; }
function f(v, d) { d = d || 2; return (v > 0 ? '+' : '') + v.toFixed(d); }
function $(id) { return document.getElementById(id); }

// ── SVG Sparkline generator ────────────────────────────────────
function sparklineSVG(prices, w, h, color) {
  w = w || 90; h = h || 24; color = color || '#58a6ff';
  if (!prices || prices.length < 2) {
    return '<svg width="'+w+'" height="'+h+'"><text x="'+w/2+'" y="'+h/2+'" fill="#484f58" font-size="9" text-anchor="middle" dominant-baseline="middle">---</text></svg>';
  }
  var mn = Math.min.apply(null, prices);
  var mx = Math.max.apply(null, prices);
  var range = mx - mn || 1;
  var pad = 2;
  var pts = [];
  for (var i = 0; i < prices.length; i++) {
    var x = pad + (i / (prices.length - 1)) * (w - 2 * pad);
    var y = (h - pad) - ((prices[i] - mn) / range) * (h - 2 * pad);
    pts.push(x.toFixed(1) + ',' + y.toFixed(1));
  }
  // Determine color from trend
  var endColor = prices[prices.length - 1] >= prices[0] ? '#00e676' : '#ff5252';
  return '<svg width="'+w+'" height="'+h+'" style="vertical-align:middle">' +
    '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + endColor + '" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<circle cx="' + pts[pts.length-1].split(',')[0] + '" cy="' + pts[pts.length-1].split(',')[1] + '" r="2" fill="' + endColor + '"/>' +
    '</svg>';
}

// ── NAV line chart ─────────────────────────────────────────────
function navChartSVG(data) {
  var w = 900, h = 80;
  if (!data || data.length < 2) {
    return '<svg width="100%" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '"><text x="' + w/2 + '" y="' + h/2 + '" fill="#484f58" font-size="12" text-anchor="middle">Collecting data...</text></svg>';
  }
  var vals = data.map(function(d) { return d.nav; });
  var mn = Math.min.apply(null, vals);
  var mx = Math.max.apply(null, vals);
  var range = mx - mn || 1;
  var pad = 4;
  var pts = [];
  var fillPts = [];
  for (var i = 0; i < vals.length; i++) {
    var x = pad + (i / (vals.length - 1)) * (w - 2 * pad);
    var y = (h - pad) - ((vals[i] - mn) / range) * (h - 2 * pad);
    pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    fillPts.push(x.toFixed(1) + ',' + y.toFixed(1));
  }
  // Close the fill polygon
  fillPts.push((w - pad).toFixed(1) + ',' + (h - pad).toFixed(1));
  fillPts.push(pad.toFixed(1) + ',' + (h - pad).toFixed(1));

  var endVal = vals[vals.length - 1];
  var startVal = vals[0];
  var lineColor = endVal >= startVal ? '#00e676' : '#ff5252';
  var fillColor = endVal >= startVal ? '#00e67615' : '#ff525215';

  // Y-axis labels
  var labels = '<text x="' + (w - 4) + '" y="12" fill="#8b949e" font-size="10" text-anchor="end" font-family="SF Mono,Fira Code,monospace">$' + mx.toFixed(2) + '</text>' +
    '<text x="' + (w - 4) + '" y="' + (h - 2) + '" fill="#8b949e" font-size="10" text-anchor="end" font-family="SF Mono,Fira Code,monospace">$' + mn.toFixed(2) + '</text>';

  // Current value label
  var lastX = pts[pts.length-1].split(',')[0];
  var lastY = pts[pts.length-1].split(',')[1];
  var curLabel = '<text x="' + (parseFloat(lastX) - 4) + '" y="' + (parseFloat(lastY) - 6) + '" fill="' + lineColor + '" font-size="11" text-anchor="end" font-weight="700" font-family="SF Mono,Fira Code,monospace">$' + endVal.toFixed(2) + '</text>';

  return '<svg width="100%" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
    '<polygon points="' + fillPts.join(' ') + '" fill="' + fillColor + '"/>' +
    '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + lineColor + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<circle cx="' + lastX + '" cy="' + lastY + '" r="3" fill="' + lineColor + '"/>' +
    labels + curLabel +
    '</svg>';
}

// ── Order book modal ───────────────────────────────────────────
function openBook(symbol) {
  $('book-overlay').classList.add('show');
  $('book-title').textContent = symbol + ' Order Book';
  $('book-sub').textContent = 'Loading...';
  $('book-content').innerHTML = '';
  fetch('/api/book/' + encodeURIComponent(symbol))
    .then(function(r) { return r.json(); })
    .then(function(d) { renderBook(d); })
    .catch(function(err) { $('book-sub').textContent = 'Error: ' + err; });
}
function closeBook(e) {
  if (e && e.target !== $('book-overlay')) return;
  $('book-overlay').classList.remove('show');
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') $('book-overlay').classList.remove('show');
});

function renderBook(d) {
  var maxQty = 1;
  d.bids.forEach(function(b) { if (b[1] > maxQty) maxQty = b[1]; });
  d.asks.forEach(function(a) { if (a[1] > maxQty) maxQty = a[1]; });

  $('book-sub').textContent = 'FV: $' + d.fv.toFixed(2) + '  |  ' + d.bids.length + ' bid levels, ' + d.asks.length + ' ask levels';

  var html = '<div class="book-fv">Fair Value: $' + d.fv.toFixed(2) + '</div>';
  html += '<div class="book-ladder">';

  // Bids side
  html += '<div class="book-side"><h3>Bids</h3>';
  for (var i = 0; i < d.bids.length; i++) {
    var b = d.bids[i];
    var pct = Math.round((b[1] / maxQty) * 100);
    html += '<div class="book-row">' +
      '<span class="price g">' + b[0].toFixed(2) + '</span>' +
      '<span class="qty">' + b[1] + '</span>' +
      '<div class="book-bar bid-bar" style="width:' + pct + '%"></div>' +
      '</div>';
  }
  if (!d.bids.length) html += '<div class="d" style="text-align:center;padding:12px">No bids</div>';
  html += '</div>';

  // Asks side
  html += '<div class="book-side"><h3>Asks</h3>';
  for (var i = 0; i < d.asks.length; i++) {
    var a = d.asks[i];
    var pct = Math.round((a[1] / maxQty) * 100);
    html += '<div class="book-row">' +
      '<span class="price r">' + a[0].toFixed(2) + '</span>' +
      '<span class="qty">' + a[1] + '</span>' +
      '<div class="book-bar ask-bar" style="width:' + pct + '%"></div>' +
      '</div>';
  }
  if (!d.asks.length) html += '<div class="d" style="text-align:center;padding:12px">No asks</div>';
  html += '</div>';

  html += '</div>';
  $('book-content').innerHTML = html;
}

// ── SSE Stream ─────────────────────────────────────────────────
var src = new EventSource('/stream');
src.onmessage = function(e) {
  var d = JSON.parse(e.data);
  $('sub').textContent = 'Live  |  ' + new Date().toLocaleTimeString();

  // ── NAV chart update ─────────────────────────────────────────
  initNavChart();
  if (navSeries) {
    navTickCount++;
    navSeries.update({ time: navTickCount, value: d.nav });
  }

  // ── Cards ────────────────────────────────────────────────────
  $('cards').innerHTML =
    '<div class="card"><div class="l">NAV</div><div class="v" style="color:#e6edf3">$' + d.nav.toLocaleString(undefined,{minimumFractionDigits:2}) + '</div></div>' +
    '<div class="card"><div class="l">Cash</div><div class="v" style="color:#58a6ff">$' + d.cash.toLocaleString(undefined,{minimumFractionDigits:2}) + '</div></div>' +
    '<div class="card"><div class="l">Unrealized P&L</div><div class="v ' + c(d.pnl) + '">' + f(d.pnl) + '</div></div>' +
    '<div class="card"><div class="l">Market P&L</div><div class="v ' + c(d.market_pl) + '">' + f(d.market_pl) + '</div></div>' +
    '<div class="card"><div class="l">Fills (M/T)</div><div class="v">' + d.maker_qty.toLocaleString() + '/' + d.taker_qty.toLocaleString() + '</div></div>' +
    '<div class="card"><div class="l">Open Orders</div><div class="v">' + d.n_orders + '</div></div>' +
    '<div class="card"><div class="l">Orders Sent</div><div class="v">' + d.total_orders.toLocaleString() + '</div></div>';

  // ── Ticker ───────────────────────────────────────────────────
  var tk = '';
  var trades = d.recent_trades.slice(0, 25);
  for (var i = 0; i < trades.length; i++) {
    var t = trades[i];
    var side = t.qty > 0 ? 'BUY' : 'SELL';
    var sc = t.qty > 0 ? 'g' : 'r';
    var ours = t.ours ? 'ours' : '';
    var ts = new Date(t.ts * 1000).toLocaleTimeString();
    tk += '<div class="ticker-row ' + ours + '"><span class="d" style="width:70px">' + ts + '</span><span class="sym">' + t.sym + '</span><span class="' + sc + '" style="width:40px">' + side + '</span><span class="n" style="width:50px">' + Math.abs(t.qty) + '</span><span class="n" style="width:70px">@' + t.px.toFixed(2) + '</span></div>';
  }
  $('ticker').innerHTML = tk;

  // ── Positions (with sparklines) ──────────────────────────────
  var ph = '<table><thead><tr><th>Team</th><th class="n">Pos</th><th class="n">FV</th><th class="n">Bid</th><th class="n">Ask</th><th class="n">Mid</th><th class="n">Edge</th><th class="n">P&L/ct</th><th class="n">Total P&L</th><th class="n">Volume</th><th>Trend</th></tr></thead><tbody>';
  for (var i = 0; i < d.positions.length; i++) {
    var r = d.positions[i];
    var pc = c(r.pnl_total);
    var bb = r.bb != null ? r.bb.toFixed(2) : '---';
    var ba = r.ba != null ? r.ba.toFixed(2) : '---';
    var mid = r.mid != null ? r.mid.toFixed(2) : '---';
    var edge = r.mid != null ? (r.fv - r.mid).toFixed(2) : '---';
    var ec = r.mid != null ? c(r.fv - r.mid) : 'd';
    ph += '<tr>' +
      '<td><span class="team-link" onclick="openBook(\'' + r.team.replace(/'/g, "\\'") + '\')">' + r.team + '</span></td>' +
      '<td class="n">' + (r.qty > 0 ? '+' : '') + r.qty + '</td>' +
      '<td class="n">' + r.fv.toFixed(2) + '</td>' +
      '<td class="n g">' + bb + '</td>' +
      '<td class="n r">' + ba + '</td>' +
      '<td class="n">' + mid + '</td>' +
      '<td class="n ' + ec + '">' + edge + '</td>' +
      '<td class="n ' + pc + '">' + f(r.pnl_per) + '</td>' +
      '<td class="n ' + pc + '">' + f(r.pnl_total) + '</td>' +
      '<td class="n d">' + r.vol.toLocaleString() + '</td>' +
      '<td class="sparkline-cell">' + sparklineSVG(r.sparkline) + '</td>' +
      '</tr>';
  }
  ph += '</tbody></table>';
  $('positions').innerHTML = ph;

  // ── Market overview (with sparklines) ────────────────────────
  var mh = '<table><thead><tr><th>Team</th><th class="n">FV</th><th class="n">Bid</th><th class="n">Ask</th><th class="n">Mid</th><th class="n">Edge</th><th class="n">Pos</th><th class="n">Volume</th><th>Trend</th></tr></thead><tbody>';
  for (var i = 0; i < d.market.length; i++) {
    var m = d.market[i];
    var bb = m.bb != null ? m.bb.toFixed(2) : '---';
    var ba = m.ba != null ? m.ba.toFixed(2) : '---';
    var mid = m.mid != null ? m.mid.toFixed(2) : '---';
    var ec = c(m.edge);
    var pc = m.pos !== 0 ? (m.pos > 0 ? 'g' : 'r') : 'd';
    mh += '<tr>' +
      '<td><span class="team-link" onclick="openBook(\'' + m.sym.replace(/'/g, "\\'") + '\')">' + m.sym + '</span></td>' +
      '<td class="n">' + m.fv.toFixed(2) + '</td>' +
      '<td class="n g">' + bb + '</td>' +
      '<td class="n r">' + ba + '</td>' +
      '<td class="n">' + mid + '</td>' +
      '<td class="n ' + ec + '">' + f(m.edge) + '</td>' +
      '<td class="n ' + pc + '">' + (m.pos !== 0 ? ((m.pos > 0 ? '+' : '') + m.pos) : '-') + '</td>' +
      '<td class="n d">' + m.vol.toLocaleString() + '</td>' +
      '<td class="sparkline-cell">' + sparklineSVG(m.sparkline) + '</td>' +
      '</tr>';
  }
  mh += '</tbody></table>';
  $('market').innerHTML = mh;

  // ── Orders ───────────────────────────────────────────────────
  var oh = '<table><thead><tr><th>Symbol</th><th>Side</th><th class="n">Price</th><th class="n">Qty</th><th>ID</th></tr></thead><tbody>';
  for (var i = 0; i < d.orders.length; i++) {
    var o = d.orders[i];
    var sc = o.side === 'BID' ? 'g' : 'r';
    oh += '<tr><td>' + o.sym + '</td><td class="' + sc + '">' + o.side + '</td><td class="n">' + o.px.toFixed(2) + '</td><td class="n">' + o.qty + '</td><td class="d" style="font-size:.7rem">' + o.id + '</td></tr>';
  }
  if (!d.orders.length) oh += '<tr><td colspan="5" class="d" style="text-align:center">No open orders</td></tr>';
  oh += '</tbody></table>';
  $('orders').innerHTML = oh;

  // ── Leaderboard ──────────────────────────────────────────────
  var lb = d.leaderboard || [];
  var lh = '<table><thead><tr><th class="n" style="width:40px">Rank</th><th class="n">User ID</th><th class="n">Est. P&L</th><th class="n">Trades</th><th class="n">Contracts</th><th></th></tr></thead><tbody>';
  for (var i = 0; i < lb.length; i++) {
    var p = lb[i];
    var rowCls = p.is_us ? 'lb-us' : '';
    var pnlCls = c(p.pnl);
    var tag = p.is_us ? '<span class="badge badge-mm" style="margin-left:8px">US</span>' : '';
    var medal = p.rank <= 3 ? ['', '#ffd700', '#c0c0c0', '#cd7f32'][p.rank] : '';
    var rankDisp = medal ? '<span style="color:' + medal + '">#' + p.rank + '</span>' : '#' + p.rank;
    lh += '<tr class="' + rowCls + '">' +
      '<td class="n lb-rank">' + rankDisp + '</td>' +
      '<td class="n">' + p.uid + tag + '</td>' +
      '<td class="n ' + pnlCls + '">' + f(p.pnl) + '</td>' +
      '<td class="n">' + p.trades.toLocaleString() + '</td>' +
      '<td class="n">' + p.n_contracts + '</td>' +
      '<td></td>' +
      '</tr>';
  }
  if (!lb.length) lh += '<tr><td colspan="6" class="d" style="text-align:center">No trade data yet</td></tr>';
  lh += '</tbody></table>';
  // Find our rank
  var ourEntry = lb.find(function(x) { return x.is_us; });
  var ourRankText = ourEntry ? 'Our rank: #' + ourEntry.rank + ' of ' + lb.length + '  |  Est. P&L: ' + f(ourEntry.pnl) : 'Not found in leaderboard';

  // ── Equity Curves Chart (lightweight-charts) ─────────────────
  var eq = d.equity_curves || [];
  var eqHtml = '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 16px;margin-bottom:16px">' +
    '<div style="font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin-bottom:6px">Competitor Equity Curves (blue = us)</div>' +
    '<div id="eq-chart" style="height:250px"></div></div>';

  $('leaderboard').innerHTML = '<div style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 16px;margin-bottom:12px;font-family:SF Mono,Fira Code,monospace;font-size:.85rem"><span class="b">' + ourRankText + '</span></div>' + eqHtml + lh;

  // Build equity chart if we have data and the container exists
  var eqEl = document.getElementById('eq-chart');
  if (eqEl && eq.length > 0 && window.LightweightCharts) {
    var ch = LightweightCharts.createChart(eqEl, {
      width: eqEl.clientWidth, height: 250,
      layout: { background: { color: '#161b22' }, textColor: '#8b949e', fontSize: 10, fontFamily: 'SF Mono, Fira Code, monospace' },
      grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
      timeScale: { visible: false },
      rightPriceScale: { borderColor: '#30363d' },
      crosshair: { mode: 0 },
    });
    var eqColors = ['#8b949e','#6e7681','#636e7b','#768390','#545d68','#e3b341','#ff5252','#00e676','#58a6ff','#d2a8ff','#79c0ff','#a5d6ff','#ffa657','#f85149','#3fb950'];
    var ci = 0;
    for (var i = 0; i < eq.length; i++) {
      var curve = eq[i];
      if (curve.uid === 634) continue; // Skip whale outlier
      if (curve.curve.length < 2) continue;
      var color = curve.is_us ? '#1f6feb' : eqColors[ci % eqColors.length];
      var width = curve.is_us ? 3 : 1;
      var series = ch.addLineSeries({ color: color, lineWidth: width, priceLineVisible: false, lastValueVisible: curve.is_us, crosshairMarkerVisible: false });
      var pts = [];
      for (var j = 0; j < curve.curve.length; j++) {
        pts.push({ time: j, value: curve.curve[j] });
      }
      series.setData(pts);
      ci++;
    }
    ch.timeScale().fitContent();
  }

  // ── Strategy panel ───────────────────────────────────────────
  var st = d.strategy || [];
  var sh = '<table><thead><tr><th>Team</th><th class="n">Pos</th><th>Strategy</th><th class="n">Hurst</th><th>Regime</th><th class="n">FV</th><th class="n">Mid</th><th class="n">Edge</th></tr></thead><tbody>';
  for (var i = 0; i < st.length; i++) {
    var s = st[i];
    var regimeBadge = s.regime === 'mean-revert' ? 'badge-mr' : s.regime === 'trending' ? 'badge-tr' : 'badge-rw';
    var hurstColor = s.hurst < 0.45 ? 'g' : s.hurst > 0.55 ? 'y' : 'd';
    var mid = s.mid != null ? s.mid.toFixed(2) : '---';
    var ec = c(s.edge);
    sh += '<tr>' +
      '<td><span class="team-link" onclick="openBook(\'' + s.team.replace(/'/g, "\\'") + '\')">' + s.team + '</span></td>' +
      '<td class="n">' + (s.qty > 0 ? '+' : '') + s.qty + '</td>' +
      '<td><span class="badge badge-mm">adaptive_mm</span></td>' +
      '<td class="n ' + hurstColor + '">' + s.hurst.toFixed(2) + '</td>' +
      '<td><span class="badge ' + regimeBadge + '">' + s.regime + '</span></td>' +
      '<td class="n">' + s.fv.toFixed(2) + '</td>' +
      '<td class="n">' + mid + '</td>' +
      '<td class="n ' + ec + '">' + f(s.edge) + '</td>' +
      '</tr>';
  }
  if (!st.length) sh += '<tr><td colspan="8" class="d" style="text-align:center">No active positions</td></tr>';
  sh += '</tbody></table>';
  $('strategy').innerHTML = sh;
};

src.onerror = function() { $('sub').textContent = 'Reconnecting...'; };
</script>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
