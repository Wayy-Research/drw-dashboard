"""Race identity: parse Kalshi markets into race keys and load polls/results.

Race keys: "GA-SEN", "AZ-GOV", "CA-H13", "WY-HAL" (at-large House seat).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from math import exp, sqrt
from typing import Any

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
POSTALS = set(STATES.values())
# Longest names first so "West Virginia" wins over "Virginia".
_STATE_RE = re.compile(r"\b(" + "|".join(sorted(STATES, key=len, reverse=True)) + r")\b", re.I)
_TICKER_RE = re.compile(r"(?:SENATE|GOVPARTY|GOVERNOR|GOV|HOUSE)([A-Z]{2})(\d{1,2}|AL)?")
_DISTRICT_RE = re.compile(
    r"\b(?:district|cd)\s*#?\s*(\d{1,2})\b"                       # "district 7"
    r"|\b([A-Z]{2})-(\d{1,2})\b"                                   # "PA-7"
    r"|\b(\d{1,2})(?:st|nd|rd|th)\s+(?:congressional\s+)?district\b",  # "7th district"
    re.I)
_PARTY_WORDS = [
    (re.compile(r"\bdemocrat(ic|s)?\b|\(d\)|^d$", re.I), "D"),
    (re.compile(r"\brepublican(s)?\b|\bgop\b|\(r\)|^r$", re.I), "R"),
    (re.compile(r"\bindependent\b|\(i\)", re.I), "I"),
]
PARTY_CODES = {"dem": "D", "d": "D", "democrat": "D", "democratic": "D",
               "gop": "R", "rep": "R", "r": "R", "republican": "R",
               "ind": "I", "i": "I", "independent": "I"}


def norm_party(p: str | None) -> str:
    p = (p or "").strip().lower()
    return PARTY_CODES.get(p, p[:1].upper() if p else "")


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def last_name(name: str) -> str:
    parts = [p for p in norm_name(name).split() if p not in {"jr", "sr", "ii", "iii", "iv"}]
    return parts[-1] if parts else ""


# --------------------------------------------------------------------------- Kalshi -> race

def parse_race(event: dict[str, Any], market: dict[str, Any] | None = None) -> str | None:
    """Best-effort race key for a Kalshi event. None for chamber-control/other markets."""
    text = " ".join(str(x) for x in (event.get("title"), event.get("sub_title"),
                                       (market or {}).get("title")) if x)
    ticker = f"{event.get('series_ticker', '')} {event.get('event_ticker', '')}".upper()
    low = text.lower()
    if "state senate" in low or "state house" in low or "legislature" in low:
        return None
    if re.search(r"governor|gubernatorial", low) or "GOV" in ticker:
        office = "GOV"
    elif "senate" in low or "SENATE" in ticker:
        office = "SEN"
    elif re.search(r"\bhouse\b|congressional|district", low) or "HOUSE" in ticker:
        office = "H"
    else:
        return None

    state = None
    m = _STATE_RE.search(text)
    if m:
        state = STATES[m.group(1).lower()]
    t = _TICKER_RE.search(ticker)
    if not state and t and t.group(1) in POSTALS:
        state = t.group(1)
    if not state:
        d = _DISTRICT_RE.search(text)
        if d and d.group(2) and d.group(2).upper() in POSTALS:
            state = d.group(2).upper()
    if not state:
        return None
    if office != "H":
        return f"{state}-{office}"
    if ("at-large" in low or "at large" in low or re.search(r"\b[A-Z]{2}-AL\b", text)
            or (t and t.group(2) == "AL")):
        return f"{state}-HAL"
    d = _DISTRICT_RE.search(text)
    if d:
        return f"{state}-H{int(d.group(1) or d.group(3) or d.group(4))}"
    if t and t.group(2) and t.group(2).isdigit():
        return f"{state}-H{int(t.group(2))}"
    return None


def parse_outcome(market: dict[str, Any]) -> dict[str, str]:
    """What a market's YES pays on: a party and/or a candidate name."""
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "").strip()
    party = ""
    # Kalshi puts the party in `subtitle` ("Republican party:: Republican party") and
    # the market title ("Will Republican win ...?"); the YES label is often the name.
    for text in (label, market.get("subtitle") or "", market.get("title") or ""):
        for rx, code in _PARTY_WORDS:
            if rx.search(text):
                party = code
                break
        if party:
            break
    if not party:
        suffix = str(market.get("ticker", "")).rsplit("-", 1)[-1].upper()
        if suffix in {"D", "DEM"}:
            party = "D"
        elif suffix in {"R", "REP", "GOP"}:
            party = "R"
    return {"party": party, "name": label}


# --------------------------------------------------------------------------- race model input

@dataclass
class Candidate:
    name: str
    party: str = ""
    poll_share: float | None = None   # weighted poll average, percentage points
    votes: float = 0.0
    winner: bool = False

    @property
    def cid(self) -> str:
        return f"{self.party}:{last_name(self.name)}" if self.name else self.party


@dataclass
class Race:
    key: str
    candidates: list[Candidate] = field(default_factory=list)
    reporting: float | None = None     # share of expected vote counted, 0-1
    n_polls: int = 0
    results_updated: str = ""

    @property
    def office(self) -> str:
        return "H" if "-H" in self.key else self.key.split("-")[-1]

    def find(self, party: str = "", name: str = "") -> Candidate | None:
        ln = last_name(name)
        # Never match a name across parties when both sides know the party.
        pool = [c for c in self.candidates if not (party and c.party and c.party != party)]
        if ln:
            for c in pool:
                if last_name(c.name) == ln:
                    return c
            for c in pool:  # label may be "Jon Ossoff (D)" or "Ossoff wins"
                if c.name and last_name(c.name) in norm_name(name).split():
                    return c
        if party:
            same = [c for c in self.candidates if c.party == party]
            if len(same) == 1:
                return same[0]
        return None

    def upsert(self, name: str, party: str) -> Candidate:
        c = self.find(party, name)
        if c is None:
            c = Candidate(name=name, party=party)
            self.candidates.append(c)
        else:
            c.party = c.party or party
            c.name = c.name or name
        return c


def _parse_date(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            pass
    return None


def apply_polls(races: dict[str, Race], rows: list[dict[str, str]], today: date,
                half_life_days: float = 10.0) -> None:
    """Weighted poll average per candidate.

    CSV columns: race, candidate, party, pct, end_date, sample_size, pollster
    Weight = sqrt(n / 600) * 0.5 ** (age_days / half_life). Rows sharing
    (race, pollster, end_date) are one poll.
    """
    sums: dict[tuple[str, str], list[float]] = {}
    polls_per_race: dict[str, set] = {}
    names: dict[tuple[str, str], tuple[str, str]] = {}
    for r in rows:
        key = (r.get("race") or "").strip().upper()
        try:
            pct = float(r.get("pct", ""))
        except ValueError:
            continue
        if not key:
            continue
        end = _parse_date(r.get("end_date", "")) or today
        age = max(0, (today - end).days)
        try:
            n = float(r.get("sample_size") or 600)
        except ValueError:
            n = 600.0
        w = sqrt(max(n, 50) / 600.0) * exp(-age * 0.6931 / half_life_days)
        name, party = r.get("candidate", "").strip(), norm_party(r.get("party"))
        ck = (key, f"{party}:{last_name(name)}" if name else party)
        s = sums.setdefault(ck, [0.0, 0.0])
        s[0] += w * pct
        s[1] += w
        names[ck] = (name, party)
        polls_per_race.setdefault(key, set()).add((r.get("pollster", ""), r.get("end_date", "")))
    for ck, (wsum, wt) in sums.items():
        race = races.setdefault(ck[0], Race(ck[0]))
        name, party = names[ck]
        race.upsert(name, party).poll_share = wsum / wt if wt else None
    for key, polls in polls_per_race.items():
        races[key].n_polls = len(polls)


def apply_results(races: dict[str, Race], rows: list[dict[str, str]]) -> None:
    """Live counts. Columns: race, candidate, party, votes, pct_reporting (0-100 share of
    EXPECTED vote counted), winner (true/1/x when the race is called), updated_at."""
    for r in rows:
        key = (r.get("race") or "").strip().upper()
        if not key:
            continue
        race = races.setdefault(key, Race(key))
        c = race.upsert(r.get("candidate", "").strip(), norm_party(r.get("party")))
        try:
            c.votes = float(r.get("votes") or 0)
        except ValueError:
            pass
        c.winner = str(r.get("winner", "")).strip().lower() in {"1", "true", "x", "yes", "y"}
        try:
            pr = float(r.get("pct_reporting", ""))
            race.reporting = max(0.0, min(1.0, pr / 100.0 if pr > 1 else pr))
        except ValueError:
            pass
        race.results_updated = r.get("updated_at", "") or race.results_updated


def ap_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Convert an AP Elections API v3 response (level=state) into results rows.

    Field names follow AP's v3 docs (races[].reportingUnits[].candidates[]);
    expected-vote share is `eevp`, falling back to precincts reporting.
    """
    rows: list[dict[str, str]] = []
    office_map = {"S": "SEN", "G": "GOV", "H": "H"}
    for race in payload.get("races") or []:
        office = office_map.get(str(race.get("officeID", "")).upper())
        if not office:
            continue
        for ru in race.get("reportingUnits") or []:
            if str(ru.get("level", "state")).lower() not in {"state", "district"}:
                continue
            st = ru.get("statePostal") or race.get("statePostal") or ""
            if office == "H":
                seat = str(race.get("seatNum") or "").strip()
                if "large" in str(race.get("seatName", "")).lower() or not seat.isdigit():
                    key = f"{st}-HAL"
                else:
                    key = f"{st}-H{int(seat)}"
            else:
                key = f"{st}-{office}"
            pr = ru.get("eevp", ru.get("precinctsReportingPct", ""))
            for c in ru.get("candidates") or []:
                rows.append({
                    "race": key, "candidate": f"{c.get('first', '')} {c.get('last', '')}".strip(),
                    "party": c.get("party", ""), "votes": str(c.get("voteCount", 0)),
                    "pct_reporting": str(pr), "winner": str(c.get("winner", "")),
                    "updated_at": ru.get("lastUpdated", ""),
                })
    return rows
