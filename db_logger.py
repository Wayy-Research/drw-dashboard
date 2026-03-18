"""DRW Market Madness -- PostgreSQL data logger.

Streams every trade, orderbook snapshot, fill, position, and P&L tick
into wayydb so we can replay, chart, and post-mortem the session.

All writes are async (asyncpg) and use connection pooling so we never
block the trading hot path.  Tables are created idempotently on init.

Usage:
    from mania.drw.db_logger import init_db, log_trades, log_fill, ...

    await init_db()
    await log_trades(trades_data)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

log = logging.getLogger("drw_db_logger")

# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------
DB_HOST = "100.96.150.42"
DB_PORT = 5432
DB_USER = "wayy"
DB_NAME = "wayydb"

_pool: asyncpg.Pool | None = None


def _get_password() -> str:
    """Resolve DB password: env var first, then ~/.config/wayy/config.json."""
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

    raise RuntimeError(
        "No DB password found. Set WAYY_DB_PASS or create "
        "~/.config/wayy/config.json with a 'db_pass' key."
    )


async def _get_pool() -> asyncpg.Pool:
    """Return (and lazily create) the connection pool."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=_get_password(),
            database=DB_NAME,
            min_size=2,
            max_size=10,
        )
        log.info("Connection pool created (%s:%s/%s)", DB_HOST, DB_PORT, DB_NAME)
    return _pool


# ---------------------------------------------------------------------------
# DDL -- all tables use CREATE TABLE IF NOT EXISTS
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS drw_trades (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(30) NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    quantity    INTEGER NOT NULL,
    maker_id    INTEGER,
    taker_id    INTEGER,
    UNIQUE (timestamp, symbol, price, quantity, maker_id, taker_id)
);

CREATE TABLE IF NOT EXISTS drw_orderbook_snapshots (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(30) NOT NULL,
    best_bid    DOUBLE PRECISION,
    best_ask    DOUBLE PRECISION,
    mid         DOUBLE PRECISION,
    spread      DOUBLE PRECISION,
    bid_depth   INTEGER,
    ask_depth   INTEGER
);

CREATE TABLE IF NOT EXISTS drw_positions (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(30) NOT NULL,
    position        INTEGER NOT NULL,
    fair_value      DOUBLE PRECISION,
    market_mid      DOUBLE PRECISION,
    unrealized_pnl  DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS drw_fills (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      VARCHAR(30) NOT NULL,
    side        VARCHAR(4) NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    quantity    INTEGER NOT NULL,
    edge        DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS drw_pnl_snapshots (
    id          SERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL,
    cash        DOUBLE PRECISION,
    nav         DOUBLE PRECISION,
    market_pl   DOUBLE PRECISION,
    n_positions INTEGER,
    n_orders    INTEGER
);

CREATE TABLE IF NOT EXISTS drw_leaderboard (
    id              SERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    user_id         INTEGER NOT NULL,
    estimated_pnl   DOUBLE PRECISION,
    n_trades        INTEGER
);

-- Indexes for time-range queries
CREATE INDEX IF NOT EXISTS idx_drw_trades_ts ON drw_trades (timestamp);
CREATE INDEX IF NOT EXISTS idx_drw_ob_ts ON drw_orderbook_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_drw_pos_ts ON drw_positions (timestamp);
CREATE INDEX IF NOT EXISTS idx_drw_fills_ts ON drw_fills (timestamp);
CREATE INDEX IF NOT EXISTS idx_drw_pnl_ts ON drw_pnl_snapshots (timestamp);
CREATE INDEX IF NOT EXISTS idx_drw_lb_ts ON drw_leaderboard (timestamp);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create all tables (idempotent). Call once at startup."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
    log.info("Database tables initialized")


async def log_trades(trades_data: dict[str, list[dict[str, Any]]]) -> int:
    """Bulk-insert market trades.

    Expects the shape returned by the DRW /trades endpoint:
        {symbol: [{price, quantity, timestamp, maker_id, taker_id}, ...], ...}

    Uses ON CONFLICT DO NOTHING to skip duplicates.
    Returns the number of rows inserted.
    """
    pool = await _get_pool()
    rows: list[tuple[Any, ...]] = []
    for symbol, tlist in trades_data.items():
        for t in tlist:
            ts = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc)
            rows.append((
                ts,
                symbol,
                float(t["price"]),
                int(t["quantity"]),
                t.get("maker_id"),
                t.get("taker_id"),
            ))

    if not rows:
        return 0

    async with pool.acquire() as conn:
        result = await conn.executemany(
            """
            INSERT INTO drw_trades (timestamp, symbol, price, quantity, maker_id, taker_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
    log.debug("Logged %d trade rows", len(rows))
    return len(rows)


async def log_orderbook_snapshot(
    books_data: dict[str, dict[str, Any]],
) -> int:
    """Snapshot the current top-of-book for every active contract.

    Expects the shape from /orderbooks:
        {symbol: {bids: {price_str: qty, ...}, asks: {price_str: qty, ...}}, ...}
    """
    pool = await _get_pool()
    now = datetime.now(timezone.utc)
    rows: list[tuple[Any, ...]] = []

    for symbol, book in books_data.items():
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}

        bid_prices = [float(p) for p, q in bids.items() if q > 0]
        ask_prices = [float(p) for p, q in asks.items() if q > 0]

        best_bid = max(bid_prices) if bid_prices else None
        best_ask = min(ask_prices) if ask_prices else None

        if best_bid is not None and best_ask is not None:
            mid = round((best_bid + best_ask) / 2, 4)
            spread = round(best_ask - best_bid, 4)
        elif best_bid is not None:
            mid = best_bid
            spread = None
        elif best_ask is not None:
            mid = best_ask
            spread = None
        else:
            mid = None
            spread = None

        bid_depth = sum(int(q) for q in bids.values()) if bids else 0
        ask_depth = sum(int(q) for q in asks.values()) if asks else 0

        rows.append((
            now, symbol, best_bid, best_ask, mid, spread, bid_depth, ask_depth
        ))

    if not rows:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO drw_orderbook_snapshots
                (timestamp, symbol, best_bid, best_ask, mid, spread, bid_depth, ask_depth)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            rows,
        )
    log.debug("Logged %d orderbook snapshots", len(rows))
    return len(rows)


async def log_positions(
    account_data: dict[str, Any],
    fair_values: dict[str, float],
    books_data: dict[str, dict[str, Any]],
) -> int:
    """Log current position state for every held contract.

    account_data: from /account  {cash: float, positions: {symbol: qty}}
    fair_values:  {symbol: fv}
    books_data:   from /orderbooks
    """
    pool = await _get_pool()
    now = datetime.now(timezone.utc)
    positions = account_data.get("positions", {})
    rows: list[tuple[Any, ...]] = []

    for symbol, qty in positions.items():
        if qty == 0:
            continue

        fv = fair_values.get(symbol)

        # Compute market mid
        book = books_data.get(symbol, {})
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}
        bid_prices = [float(p) for p, q in bids.items() if q > 0]
        ask_prices = [float(p) for p, q in asks.items() if q > 0]
        best_bid = max(bid_prices) if bid_prices else None
        best_ask = min(ask_prices) if ask_prices else None

        if best_bid is not None and best_ask is not None:
            market_mid = round((best_bid + best_ask) / 2, 4)
        elif best_bid is not None:
            market_mid = best_bid
        elif best_ask is not None:
            market_mid = best_ask
        else:
            market_mid = None

        # Unrealized P&L: (fv - mid) * qty  (positive when long and undervalued)
        if fv is not None and market_mid is not None:
            unrealized_pnl = round((fv - market_mid) * qty, 4)
        else:
            unrealized_pnl = None

        rows.append((now, symbol, int(qty), fv, market_mid, unrealized_pnl))

    if not rows:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO drw_positions
                (timestamp, symbol, position, fair_value, market_mid, unrealized_pnl)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            rows,
        )
    log.debug("Logged %d position snapshots", len(rows))
    return len(rows)


async def log_fill(
    symbol: str,
    side: str,
    price: float,
    qty: int,
    edge: float | None = None,
) -> None:
    """Log one of our fills."""
    pool = await _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO drw_fills (timestamp, symbol, side, price, quantity, edge)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            now, symbol, side[:4], float(price), int(qty), edge,
        )
    log.debug("Logged fill: %s %s %d @ %.2f", side, symbol, qty, price)


async def log_pnl(
    cash: float,
    nav: float,
    market_pl: float,
    n_positions: int,
    n_orders: int,
) -> None:
    """Log a P&L snapshot for charting."""
    pool = await _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO drw_pnl_snapshots
                (timestamp, cash, nav, market_pl, n_positions, n_orders)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            now, float(cash), float(nav), float(market_pl),
            int(n_positions), int(n_orders),
        )
    log.debug("Logged P&L: nav=%.2f market_pl=%.2f", nav, market_pl)


async def log_leaderboard(
    participants: list[dict[str, Any]] | dict[str, Any],
) -> int:
    """Log estimated leaderboard state.

    participants: list of {user_id, estimated_pnl, n_trades}
                  or from the /reports endpoint (adapt as needed)
    """
    pool = await _get_pool()
    now = datetime.now(timezone.utc)

    # Normalize to list
    if isinstance(participants, dict):
        participants = [participants]

    rows: list[tuple[Any, ...]] = []
    for p in participants:
        rows.append((
            now,
            int(p.get("user_id", p.get("id", 0))),
            float(p.get("estimated_pnl", p.get("market_pl", 0.0))),
            int(p.get("n_trades", p.get("total_orders", 0))),
        ))

    if not rows:
        return 0

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO drw_leaderboard
                (timestamp, user_id, estimated_pnl, n_trades)
            VALUES ($1, $2, $3, $4)
            """,
            rows,
        )
    log.debug("Logged %d leaderboard entries", len(rows))
    return len(rows)


async def close_pool() -> None:
    """Gracefully close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("Connection pool closed")


# ---------------------------------------------------------------------------
# Quick smoke test
# ---------------------------------------------------------------------------
async def _smoke_test() -> None:
    """Connect, create tables, insert a test row, verify, clean up."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    log.info("Running smoke test against %s:%s/%s", DB_HOST, DB_PORT, DB_NAME)

    # 1. Init tables
    await init_db()
    log.info("Tables created successfully")

    # 2. Test P&L insert
    await log_pnl(cash=10000.0, nav=10500.0, market_pl=500.0, n_positions=5, n_orders=42)
    log.info("P&L snapshot inserted")

    # 3. Test fill insert
    await log_fill(symbol="Duke", side="BUY", price=22.50, qty=10, edge=0.20)
    log.info("Fill inserted")

    # 4. Verify rows
    pool = await _get_pool()
    async with pool.acquire() as conn:
        pnl_count = await conn.fetchval("SELECT count(*) FROM drw_pnl_snapshots")
        fill_count = await conn.fetchval("SELECT count(*) FROM drw_fills")
        trade_count = await conn.fetchval("SELECT count(*) FROM drw_trades")
        ob_count = await conn.fetchval("SELECT count(*) FROM drw_orderbook_snapshots")
        pos_count = await conn.fetchval("SELECT count(*) FROM drw_positions")
        lb_count = await conn.fetchval("SELECT count(*) FROM drw_leaderboard")

    log.info(
        "Row counts -- pnl: %d, fills: %d, trades: %d, ob: %d, positions: %d, leaderboard: %d",
        pnl_count, fill_count, trade_count, ob_count, pos_count, lb_count,
    )

    await close_pool()
    log.info("Smoke test passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
