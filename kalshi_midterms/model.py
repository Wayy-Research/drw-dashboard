"""Win probabilities from polls + live counts.

Everything is done on the margin (candidate share minus strongest rival,
in percentage points), modelled as normal:

  r            = share of the EXPECTED vote already counted (0-1)
  M_counted    = margin among counted votes
  M_poll       = polling-average margin
  remaining    = margin of the votes still out. Its mean blends the counted
                 margin and the poll margin, trusting the count more as r grows:
                 mu_rem = r*M_counted + (1-r)*M_poll
  final mean   = r*M_counted + (1-r)*mu_rem
  final sd     = (1-r) * (sigma_poll*(1-r) + sigma_late*r)  (+ a small floor)

  r = 0  -> pure polling model (sd = historical polling error)
  r -> 1 -> sd shrinks, but sigma_late keeps late-counted ballots
            (mail, provisional) able to swing a close race.

P(candidate wins) = Phi(final mean / final sd), normalised across the race.
A called race (winner flag) gets `called_prob` for the winner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import erf, sqrt

from .races import Race


@dataclass
class ModelParams:
    sigma_poll: dict[str, float] = field(default_factory=lambda: {"SEN": 5.5, "GOV": 5.5, "H": 7.0})
    sigma_late: float = 15.0      # how differently the uncounted vote can break, in points
    sd_floor: float = 0.25        # count/recount noise
    sigma_no_poll: float = 12.0   # prior sd when there are counts but no polls
    called_prob: float = 0.99     # leave room for settlement / recount risk


def phi(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def win_probabilities(race: Race, params: ModelParams | None = None) -> dict[str, dict]:
    """{candidate cid: {"p": prob, "basis": str, "mean": margin, "sd": sd}}. Empty if no data."""
    params = params or ModelParams()
    cands = race.candidates
    if not cands:
        return {}

    winners = [c for c in cands if c.winner]
    if winners:
        w = winners[0]
        others = [c for c in cands if c is not w]
        rest = (1 - params.called_prob) / max(1, len(others))
        return {c.cid: {"p": params.called_prob if c is w else rest, "basis": "called",
                        "mean": None, "sd": None} for c in cands}

    total_votes = sum(c.votes for c in cands)
    has_counts = total_votes > 0 and race.reporting is not None
    has_polls = any(c.poll_share is not None for c in cands)
    if not has_counts and not has_polls:
        return {}

    r = race.reporting if has_counts else 0.0
    sigma_prior = params.sigma_poll.get(race.office, 6.0) if has_polls else params.sigma_no_poll
    sd = (1 - r) * (sigma_prior * (1 - r) + params.sigma_late * r)
    sd = sqrt(sd * sd + params.sd_floor ** 2)

    def counted(c) -> float:
        return 100.0 * c.votes / total_votes if has_counts else 0.0

    def polled(c) -> float | None:
        return c.poll_share

    # Expected final share for each candidate.
    expected: dict[str, float] = {}
    for c in cands:
        s_cnt, s_poll = counted(c), polled(c)
        if has_counts and s_poll is not None:
            mu_rem = r * s_cnt + (1 - r) * s_poll
        elif has_counts:
            mu_rem = s_cnt
        else:
            mu_rem = s_poll if s_poll is not None else 0.0
        expected[c.cid] = (r * s_cnt + (1 - r) * mu_rem) if has_counts else mu_rem

    basis = "polls+count" if has_counts and has_polls else ("count" if has_counts else "polls")
    raw: dict[str, tuple[float, float]] = {}
    for c in cands:
        rivals = [v for k, v in expected.items() if k != c.cid]
        margin = expected[c.cid] - (max(rivals) if rivals else 0.0)
        raw[c.cid] = (phi(margin / sd), margin)
    z = sum(p for p, _ in raw.values()) or 1.0
    return {cid: {"p": p / z, "basis": basis, "mean": round(m, 2), "sd": round(sd, 2)}
            for cid, (p, m) in raw.items()}
