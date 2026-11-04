"""HTTP server + background refresh loop.

Environment:
  POLLS_SOURCE      CSV/JSON file path or URL of polls (see README)
  RESULTS_SOURCE    CSV/JSON file path or URL of live counts, or "ap" for the AP API
  AP_API_KEY        AP Elections API key (when RESULTS_SOURCE=ap)
  AP_ELECTION_DATE  default 2026-11-03
  OVERRIDES_FILE    JSON {kalshi_market_ticker: {"race": "GA-SEN", "party": "D", "candidate": "..."}}
  KALSHI_SERIES     comma list of Kalshi series tickers to restrict to (skips discovery)
  REFRESH_SECONDS   default 60 (markets + results); polls refresh every 10 minutes
  SNAPSHOT_FILE     optional path; each snapshot is also written there as JSON
  HOST / PORT       default 0.0.0.0 / 8090
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import kalshi
from .fetch import get_json, load_rows
from .races import Race, ap_rows, apply_polls, apply_results
from .value import ScanParams, scan

log = logging.getLogger("kalshi_midterms")

REFRESH = float(os.environ.get("REFRESH_SECONDS", "60"))
POLL_REFRESH = 600.0
PAGE = (Path(__file__).parent / "page.html")

_lock = threading.Lock()
_state: dict[str, Any] = {"snapshot": None, "sources": {}}
_cache: dict[str, Any] = {"polls": [], "polls_at": 0.0}


def _status(name: str, ok: bool, detail: str) -> None:
    with _lock:
        _state["sources"][name] = {"ok": ok, "detail": detail,
                                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _load_polls() -> list[dict[str, str]]:
    src = os.environ.get("POLLS_SOURCE", "").strip()
    if not src:
        _status("polls", False, "POLLS_SOURCE not set")
        return []
    if time.time() - _cache["polls_at"] < POLL_REFRESH and _cache["polls"]:
        return _cache["polls"]
    try:
        rows = load_rows(src)
        _cache.update(polls=rows, polls_at=time.time())
        _status("polls", True, f"{len(rows)} rows")
    except Exception as exc:  # noqa: BLE001
        _status("polls", False, f"{type(exc).__name__}: {exc}")
    return _cache["polls"]


def _load_results() -> list[dict[str, str]]:
    src = os.environ.get("RESULTS_SOURCE", "").strip()
    if not src:
        _status("results", False, "RESULTS_SOURCE not set")
        return []
    try:
        if src.lower() == "ap":
            key = os.environ.get("AP_API_KEY", "")
            if not key:
                raise RuntimeError("AP_API_KEY not set")
            day = os.environ.get("AP_ELECTION_DATE", "2026-11-03")
            payload = get_json(f"https://api.ap.org/v3/elections/{day}",
                               {"format": "json", "level": "state", "officeID": "S,G,H",
                                "resultsType": "l"}, headers={"x-api-key": key})
            rows = ap_rows(payload)
        else:
            rows = load_rows(src)
        _status("results", True, f"{len(rows)} rows")
        return rows
    except Exception as exc:  # noqa: BLE001
        _status("results", False, f"{type(exc).__name__}: {exc}")
        return []


def _load_overrides() -> dict[str, dict]:
    path = os.environ.get("OVERRIDES_FILE", "").strip()
    if not path:
        return {}
    try:
        return {k.upper(): v for k, v in json.loads(Path(path).read_text()).items()}
    except Exception as exc:  # noqa: BLE001
        _status("overrides", False, f"{type(exc).__name__}: {exc}")
        return {}


def refresh_once() -> None:
    try:
        events = kalshi.fetch_events()
        n_mk = sum(len(e.get("markets") or []) for e in events)
        _status("kalshi", True, f"{len(events)} events, {n_mk} markets")
    except Exception as exc:  # noqa: BLE001
        _status("kalshi", False, f"{type(exc).__name__}: {exc}")
        events = []

    races: dict[str, Race] = {}
    apply_polls(races, _load_polls(), date.today())
    apply_results(races, _load_results())
    result = scan(events, races, _load_overrides(), params=ScanParams())

    race_rows = []
    for key, r in sorted(races.items()):
        race_rows.append({"race": key, "reporting": r.reporting, "n_polls": r.n_polls,
                          "candidates": [{"name": c.name, "party": c.party,
                                          "poll_share": c.poll_share, "votes": c.votes,
                                          "winner": c.winner} for c in r.candidates]})
    with _lock:
        _state["snapshot"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "markets": sum(len(e.get("markets") or []) for e in events),
            **result, "races": race_rows, "sources": dict(_state["sources"]),
        }
        snap = _state["snapshot"]
    out = os.environ.get("SNAPSHOT_FILE", "").strip()
    if out:  # optional: mirror each snapshot to disk for static publishing
        tmp = Path(out).with_suffix(".tmp")
        tmp.write_text(json.dumps(snap))
        tmp.replace(out)


def _loop() -> None:
    while True:
        started = time.time()
        try:
            refresh_once()
        except Exception:  # noqa: BLE001 - keep serving the last good snapshot
            log.exception("refresh failed")
        time.sleep(max(1.0, REFRESH - (time.time() - started)))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/snapshot":
            with _lock:
                snap = _state["snapshot"] or {"pending": True, "sources": _state["sources"]}
            self._send(200, json.dumps(snap).encode(), "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug(fmt, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=_loop, daemon=True).start()
    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8090"))
    log.info("Kalshi midterm dashboard on http://%s:%d", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
