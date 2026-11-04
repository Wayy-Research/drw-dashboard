"""Tiny stdlib HTTP/file loader shared by every data source."""

from __future__ import annotations

import csv
import io
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "kalshi-midterms-dashboard/1.0"
TIMEOUT = 15


def get_text(source: str, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> str:
    """Read a URL (http/https) or a local file path and return its text."""
    if source.startswith(("http://", "https://")):
        url = source
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json, text/csv, */*",
            **(headers or {})})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8")
    return Path(source).expanduser().read_text(encoding="utf-8")


def get_json(source: str, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> Any:
    return json.loads(get_text(source, params, headers))


def load_rows(source: str) -> list[dict[str, str]]:
    """Load tabular rows from a CSV or JSON (list of objects) file/URL."""
    text = get_text(source)
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        data = json.loads(stripped)
        if isinstance(data, dict):
            data = data.get("rows") or data.get("data") or []
        return [{k: ("" if v is None else str(v)) for k, v in row.items()} for row in data]
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
