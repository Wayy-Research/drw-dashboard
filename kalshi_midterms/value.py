"""Fees, edge, Kelly sizing and the market scan."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from . import kalshi
from .model import ModelParams, win_probabilities
from .races import Race, parse_outcome, parse_race


@dataclass
class ScanParams:
    fee_rate: float = 0.07        # Kalshi taker fee: ceil(0.07 * C * P * (1-P)) dollars
    fee_contracts: int = 100      # position size used to round the fee
    longshot_max: float = 0.25    # ask at or below this counts as a long shot
    max_spread: float = 0.10      # wider than this is flagged as illiquid
    min_volume: float = 100       # lifetime contracts traded
    min_arb_profit: float = 0.005 # ignore bundles that clear by less than half a cent


def taker_fee(price: float, contracts: int = 100, rate: float = 0.07) -> float:
    """Per-contract Kalshi taker fee in dollars, rounded up to the cent for the order."""
    total = rate * contracts * price * (1 - price)
    return math.ceil(round(total * 100, 6)) / 100 / contracts


def evaluate_side(p_win: float, ask: float | None, bid: float | None,
                  params: ScanParams) -> dict[str, Any] | None:
    """Value of buying one contract that pays $1 with probability p_win at `ask`."""
    if ask is None:
        return None
    fee = taker_fee(ask, params.fee_contracts, params.fee_rate)
    cost = ask + fee
    edge = p_win - cost
    return {
        "ask": ask, "bid": bid, "fee": round(fee, 4), "cost": round(cost, 4),
        "model_p": round(p_win, 4), "edge": round(edge, 4),
        "ev_per_dollar": round(edge / cost, 4) if cost > 0 else None,  # expected return on stake
        "payout_multiple": round(1 / cost, 2) if cost > 0 else None,
        "kelly": round(max(0.0, edge / (1 - cost)), 4) if cost < 1 else 0.0,
        "spread": round(ask - bid, 4) if bid is not None else None,
        "longshot": ask <= params.longshot_max,
    }


def scan(events: list[dict], races: dict[str, Race], overrides: dict[str, dict] | None = None,
         model: ModelParams | None = None, params: ScanParams | None = None) -> dict[str, Any]:
    """Match every Kalshi market to a race/candidate and price both sides."""
    params, overrides = params or ScanParams(), overrides or {}
    probs = {key: win_probabilities(r, model) for key, r in races.items()}
    rows: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for ev in events:
        for m in ev.get("markets") or []:
            if m.get("status") not in (None, "active", "open"):
                continue
            ticker = m.get("ticker", "")
            ov = overrides.get(ticker, {})
            race_key = (ov.get("race") or parse_race(ev, m) or "").upper()
            outcome = parse_outcome(m)
            party = ov.get("party", outcome["party"])
            name = ov.get("candidate", outcome["name"])
            q = kalshi.quote(m)
            base = {"ticker": ticker, "event": ev.get("event_ticker", ""),
                    "title": ev.get("title", ""), "outcome": outcome["name"] or party,
                    "race": race_key, "volume": q["volume"], "volume_24h": q["volume_24h"],
                    "open_interest": q["open_interest"], "last": q["last"],
                    "close_time": m.get("close_time", "")}

            race = races.get(race_key)
            cand = race.find(party, name) if race else None
            p = probs.get(race_key, {}).get(cand.cid) if cand else None
            if p is None:
                unmatched.append({**base, "why": "no race key" if not race_key else
                                  "no polls/results for race" if not race else
                                  "candidate not found" if not cand else "no model data",
                                  "yes_ask": q["yes_ask"], "yes_bid": q["yes_bid"]})
                continue

            context = {**base, "candidate": cand.name or cand.party, "party": cand.party,
                       "basis": p["basis"], "model_mean_margin": p["mean"], "model_sd": p["sd"],
                       "reporting": race.reporting, "n_polls": race.n_polls,
                       "poll_share": cand.poll_share, "votes": cand.votes,
                       "market_p": q["last"] if q["last"] else
                       ((q["yes_bid"] + q["yes_ask"]) / 2 if q["yes_bid"] and q["yes_ask"] else None)}
            for side, pw, ask, bid in (("YES", p["p"], q["yes_ask"], q["yes_bid"]),
                                       ("NO", 1 - p["p"], q["no_ask"], q["no_bid"])):
                ev_side = evaluate_side(pw, ask, bid, params)
                if ev_side is None:
                    continue
                flags = []
                if ev_side["spread"] is None or ev_side["spread"] > params.max_spread:
                    flags.append("wide spread")
                if q["volume"] < params.min_volume:
                    flags.append("thin volume")
                if p["basis"] == "called":
                    flags.append("race called")
                rows.append({**context, "side": side, **ev_side, "flags": flags})

    rows.sort(key=lambda r: r["edge"], reverse=True)
    return {"opportunities": rows, "unmatched": unmatched,
            "arbitrage": find_arbitrage(events, params)}


def find_arbitrage(events: list[dict], params: ScanParams | None = None) -> list[dict[str, Any]]:
    """Model-free mispricings inside one race (one market per candidate/party).

    YES bundle: buy YES on every outcome; exactly one pays $1.
    NO bundle:  buy NO on every outcome; all but one pay $1, so it returns n-1.
    Both assume the listed outcomes cover every possible winner.
    """
    params = params or ScanParams()
    out: list[dict[str, Any]] = []
    for ev in events:
        mks = [m for m in ev.get("markets") or [] if m.get("status") in (None, "active", "open")]
        if len(mks) < 2:
            continue
        quotes = [kalshi.quote(m) for m in mks]
        fee = lambda p: taker_fee(p, params.fee_contracts, params.fee_rate)  # noqa: E731
        legs = [{"ticker": m.get("ticker"), "outcome": parse_outcome(m)["name"],
                 "yes_ask": q["yes_ask"], "no_ask": q["no_ask"]} for m, q in zip(mks, quotes)]
        base = {"event": ev.get("event_ticker", ""), "title": ev.get("title", ""),
                "race": parse_race(ev, mks[0]) or "", "legs": legs}
        if all(q["yes_ask"] is not None for q in quotes):
            cost = sum(q["yes_ask"] + fee(q["yes_ask"]) for q in quotes)
            if 1 - cost >= params.min_arb_profit:
                out.append({**base, "kind": "YES bundle", "cost": round(cost, 4),
                            "payout": 1.0, "profit": round(1 - cost, 4)})
        if all(q["no_ask"] is not None for q in quotes):
            cost = sum(q["no_ask"] + fee(q["no_ask"]) for q in quotes)
            payout = len(quotes) - 1
            if payout - cost >= params.min_arb_profit:
                out.append({**base, "kind": "NO bundle", "cost": round(cost, 4),
                            "payout": float(payout), "profit": round(payout - cost, 4)})
    out.sort(key=lambda a: a["profit"], reverse=True)
    return out
