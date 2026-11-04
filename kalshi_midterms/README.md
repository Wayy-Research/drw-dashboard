# Kalshi Midterm Value Scanner

Finds Kalshi election contracts priced below their modelled probability, with a
dedicated **Long shots** view: contracts asking ≤ 25¢ whose model probability
beats the price after Kalshi's taker fee.

It pulls three things every 30 seconds:

| Input | Source | Config |
|---|---|---|
| Kalshi prices | Kalshi public API (no key needed) | automatic; `KALSHI_SERIES` to restrict |
| Polls | Any CSV/JSON file or URL in the format below | `POLLS_SOURCE` |
| Live vote count | AP Elections API, or any CSV/JSON file or URL | `RESULTS_SOURCE` (+ `AP_API_KEY`) |

Standard library only; no `pip install`.

## Run

```bash
POLLS_SOURCE=polls.csv RESULTS_SOURCE=ap AP_API_KEY=... python -m kalshi_midterms
# open http://localhost:8090  (works on a phone)
```

Docker:

```bash
docker build -f Dockerfile.kalshi -t kalshi-midterms .
docker run -p 8090:8090 -v $PWD/data:/data \
  -e POLLS_SOURCE=/data/polls.csv -e RESULTS_SOURCE=/data/results.csv kalshi-midterms
```

### Render (free, no computer needed)

`render.yaml` at the repo root is a Render Blueprint. On Render: **New → Blueprint**,
pick this repo, then fill in `POLLS_SOURCE` / `RESULTS_SOURCE` (both optional;
leave blank to start with Kalshi prices only). Render gives you a public
`https://….onrender.com` URL.

Render's disk resets on every deploy, so give data sources as **URLs**. A Google
Sheet works well: *File → Share → Publish to web → CSV*, then paste the link.
A CSV committed to this repo also works (e.g. `kalshi_midterms/data/polls.csv`),
and Render redeploys when you push it.

The free plan sleeps after 15 minutes without visitors and takes about a minute
to wake. Data refreshes only while the page is awake.

## Data formats

Race keys: `GA-SEN`, `AZ-GOV`, `CA-H13` (House district), `WY-HAL` (at-large seat).
Templates are in `data/`.

**Polls** (`polls.template.csv`): one row per candidate per poll.

```
race,candidate,party,pct,end_date,sample_size,pollster
```

Each poll is weighted by `sqrt(sample_size/600)` with a 10-day half-life.

**Results** (`results.template.csv`): one row per candidate. `pct_reporting` is
the share of the *expected* vote counted (0–100). Set `winner` to `x` once the
race is called.

```
race,candidate,party,votes,pct_reporting,winner,updated_at
```

`RESULTS_SOURCE=ap` reads the AP Elections API v3 (`level=state`, offices S/G/H)
using `eevp` (expected-vote %) and AP's `winner` flag. AP access needs a paid key.
The field mapping follows AP's docs but hasn't been checked against a live feed.

**Overrides** (`OVERRIDES_FILE`): pin any Kalshi market the auto-matcher can't
read (it appears under the *Unmatched* tab with the reason):

```json
{"KALSHI-MARKET-TICKER": {"race": "GA-SEN", "party": "D", "candidate": "Full Name"}}
```

## Model

Margins (candidate − strongest rival, in points) are modelled as normal.
`r` = share of expected vote counted.

- Mean of the uncounted vote = `r·counted margin + (1−r)·poll margin`
- Final margin mean = `r·counted + (1−r)·uncounted mean`
- SD = `(1−r)·(σ_poll·(1−r) + σ_late·r)`, with σ_poll = 5.5 (Senate/Governor),
  7.0 (House), σ_late = 15
- Called races get 99% for the winner.

Before any votes are counted this is a pure polling model. As the count
advances it moves toward the count, while σ_late keeps late mail/provisional
ballots able to flip a close race. Parameters are in `model.py`.

**Edge** = model probability − (ask + fee), with fee = `ceil(0.07·C·P·(1−P))`
for C = 100 contracts. **Kelly** is full Kelly; size well below it.
Contracts with a spread over 10¢ or under 100 contracts traded are flagged as
illiquid and hidden by default.

The model is only as good as the polls and counts you feed it. A large edge on a
long shot usually means bad inputs or a market that knows something the model
doesn't; check it before trading.

## Tests

```bash
python -m unittest discover -s kalshi_midterms/tests -t .
```
