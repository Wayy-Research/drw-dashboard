"""Centralized configuration for DRW Market Madness trading system."""

from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXTERNAL_DIR = DATA_DIR / "external"
PROCESSED_DIR = DATA_DIR / "processed"
SUBMISSIONS_DIR = PROJECT_ROOT / "submissions"

# ── Kaggle Competition ────────────────────────────────────────────────
COMPETITION_NAME = "march-machine-learning-mania-2026"  # Kaggle slug
CURRENT_SEASON = 2026  # Season we're predicting (2025-26 season)

# Team ID ranges
MEN_ID_MIN = 1000
MEN_ID_MAX = 1999
WOMEN_ID_MIN = 3000
WOMEN_ID_MAX = 3999

# ── Model constants ───────────────────────────────────────────────────
CLIP_LOW = 0.02
CLIP_HIGH = 0.98

# CV seasons (leave-one-season-out)
CV_SEASONS = [2021, 2022, 2023, 2024, 2025]

# Elo parameters
ELO_INITIAL = 1500.0
ELO_K = 32.0
ELO_HOME_ADVANTAGE = 100.0

# ── File prefixes ─────────────────────────────────────────────────────
MEN_PREFIX = "M"
WOMEN_PREFIX = "W"

# ══════════════════════════════════════════════════════════════════════
# DRW Market Madness Trading Configuration
# ══════════════════════════════════════════════════════════════════════

# ── Connection ────────────────────────────────────────────────────────
GAME_ID = 160
BASE_URL = "https://games.drw.com"
TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJyaWNrQHdheXlyZXNlYXJjaC5jb20iLCJleHAiOjE3NzYzNTg4ODZ9"
    ".p7K26e4opeDJMLYJo6saDgghGabIlqyNet5lXQwz_HE"
)
OUR_USER_ID = 531

# ── Tournament structure ──────────────────────────────────────────────
SETTLEMENT_VALUES = {
    "r64_loss": 0,     # Lose in Round of 64
    "r32_loss": 2,     # Lose in Round of 32
    "s16_loss": 4,     # Lose in Sweet 16
    "e8_loss": 8,      # Lose in Elite 8
    "f4_loss": 16,     # Lose in Final Four
    "final_loss": 32,  # Lose in Championship
    "champion": 64,    # Win it all
}

# The sum of all 68 contracts always settles to exactly 320
TOTAL_SETTLEMENT = 320

# Tournament dates (March 23-27, 2026 competition window)
COMPETITION_START = "2026-03-23"
COMPETITION_END = "2026-03-27"
COMPETITION_DAYS = 5

# ── Strategy Parameters ──────────────────────────────────────────────
# Position limits
MAX_POSITION = 50       # Per-contract hard limit
QUOTE_SIZE = 5          # Default order size

# Avellaneda-Stoikov parameters
AS_GAMMA_CONSERVATIVE = 0.10  # Risk aversion when leading
AS_GAMMA_AGGRESSIVE = 0.01    # Risk aversion when trailing
AS_GAMMA_DEFAULT = 0.05       # Default risk aversion
AS_K_DEFAULT = 1.5            # Order book liquidity density

# Edge thresholds
MIN_EDGE_PCT = 0.03      # 3% of FV minimum edge to quote
SNIPE_EDGE_PCT = 0.08    # 8% of FV to aggressively cross (IOC)
POSITION_SKEW = 0.015    # Skew per contract of inventory

# Timing
REQUOTE_INTERVAL = 15    # Seconds between full requotes
SNIPE_COOLDOWN = 2.0     # Seconds between snipe scans
ORDER_RATE_LIMIT = 0.25  # Seconds between order submissions

# Risk
PNL_FLOOR = -400_000     # Stop trading if we hit this
KELLY_FRACTION = 0.25    # Fraction of Kelly optimal to use

# ── Fair Values (from 100K Monte Carlo bracket sim) ──────────────────
# These are the INITIAL fair values. They get updated live as games resolve.
INITIAL_FAIR_VALUES: dict[str, float] = {
    "Duke": 22.30,
    "Michigan": 20.87,
    "Arizona": 17.78,
    "Florida": 16.93,
    "Houston": 12.96,
    "Iowa St": 11.46,
    "Purdue": 10.14,
    "UConn": 9.93,
    "Gonzaga": 7.13,
    "Illinois": 7.11,
    "Michigan St": 5.23,
    "Virginia": 5.08,
    "Arkansas": 4.19,
    "Nebraska": 4.01,
    "Texas Tech": 3.65,
    "St Johns": 3.54,
    "Kansas": 3.26,
    "Wisconsin": 3.14,
    "Alabama": 2.94,
    "Louisville": 2.93,
    "Vanderbilt": 2.81,
    "BYU": 2.68,
    "Tennessee": 2.66,
    "Miami FL": 2.39,
    "North Carolina": 2.21,
    "Saint Marys": 2.08,
    "UCLA": 2.04,
    "Santa Clara": 1.88,
    "Saint Louis": 1.87,
    "Iowa": 1.77,
    "Villanova": 1.71,
    "Kentucky": 1.70,
    "Georgia": 1.38,
    "TCU": 1.36,
    "Clemson": 1.35,
    "Ohio St": 1.24,
    "Miami OH": 1.26,
    "High Point": 1.10,
    "McNeese": 1.10,
    "Texas A&M": 1.08,
    "South Florida": 1.04,
    "VCU": 1.04,
    "UCF": 1.23,
    "Akron": 0.97,
    "Missouri": 0.87,
    "Northern Iowa": 0.87,
    "NC State": 0.64,
    "Texas": 0.55,
    "SMU": 0.50,
    "Hofstra": 0.81,
    "Hawaii": 0.49,
    "Penn": 0.36,
    "Wright St": 0.48,
    "Troy": 0.31,
    "North Dakota St": 0.18,
    "Cal Baptist": 0.71,
    "Tennessee St": 0.23,
    "Utah St": 1.48,
    "Furman": 0.05,
    "Kennesaw St": 0.16,
    "Idaho": 0.03,
    "Siena": 0.01,
    "Long Island": 0.02,
    "Howard": 0.02,
    "UMBC": 0.00,
    "Queens": 0.05,
    "Prairie View": 0.00,
    "Lehigh": 0.02,
}

# ── Regional bracket structure (for correlation signals) ─────────────
# Maps region name to region code used by the exchange.
# Used for cross-contract correlation: if a team is eliminated,
# remaining teams in that region see FV changes.
REGIONS = {
    "East": "W",
    "South": "X",
    "Midwest": "Y",
    "West": "Z",
}

# ── Taker-only contracts ─────────────────────────────────────────────
# On these contracts we ONLY take (snipe + extract), never post resting
# limit orders. Determined from trade data: these are contracts where
# our market-making loses money, or where FV is too low for MM to be
# profitable (wide spreads, adverse selection from informed takers).
#
# Strategy: let others provide liquidity, we sweep mispricings.
TAKER_ONLY: set[str] = {
    # Losing MM contracts (negative realized cash flow as maker)
    "Duke", "Vanderbilt", "UCLA", "Missouri",
    # Low-FV junk — only worth extracting extreme mispricings
    "UMBC", "Prairie View", "Lehigh", "Siena", "Howard", "Long Island",
    "Idaho", "Furman", "Kennesaw St", "North Dakota St", "Queens",
    "Tennessee St", "Troy", "Penn", "Wright St", "Hawaii",
}
