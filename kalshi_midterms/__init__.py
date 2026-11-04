"""Kalshi midterm value-bet scanner.

Pulls Kalshi election markets, polling averages and live vote counts,
turns polls + counts into a win probability per candidate, and flags
contracts where that probability beats the Kalshi ask after fees --
with a dedicated view for long shots.

Standard library only. Run with:  python -m kalshi_midterms
"""
