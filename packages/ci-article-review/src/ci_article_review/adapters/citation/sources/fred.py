"""FRED (Federal Reserve Economic Data) / BLS resolver."""

import requests

from ci_core.http import DEFAULT_HEADERS
import logging
import os

FRED_BASE = "https://api.stlouisfed.org/fred"
log = logging.getLogger(__name__)


def resolve(claim, api_key=None):
    api_key = api_key or os.getenv("FRED_API_KEY")
    if not api_key:
        log.debug("FRED_API_KEY not set; skipping FRED resolution")
        return {"found": False}

    series_id = _guess_series_id(claim)
    if not series_id:
        return {"found": False}

    try:
        url = f"{FRED_BASE}/series/observations?series_id={series_id}&api_key={api_key}&file_type=json&limit=5&sort_order=desc"
        resp = requests.get(url, timeout=15, headers=DEFAULT_HEADERS)
        if resp.status_code == 200:
            data = resp.json()
            observations = data.get("observations", [])
            if observations:
                summary = f"Series {series_id}: latest value {observations[0].get('value')} on {observations[0].get('date')}"
                return {
                    "found": True,
                    "url": f"https://fred.stlouisfed.org/series/{series_id}",
                    "summary": summary,
                    "content": str(observations[:5]),
                }
    except Exception as e:
        log.debug(f"FRED resolve failed: {e}")

    return {"found": False}


def _guess_series_id(claim):
    """Map common economic terms to FRED series IDs."""
    mappings = {
        "unemployment": "UNRATE",
        "cpi": "CPIAUCSL",
        "inflation": "CPIAUCSL",
        "gdp": "GDP",
        "federal funds rate": "FEDFUNDS",
        "mortgage": "MORTGAGE30US",
        "housing starts": "HOUST",
    }
    claim_lower = claim.lower()
    for term, series in mappings.items():
        if term in claim_lower:
            return series
    return None
