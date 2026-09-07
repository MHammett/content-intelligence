"""EIA (Energy Information Administration) data resolver."""

import os
import requests

from ci_core.http import DEFAULT_HEADERS
import logging

EIA_BASE = "https://api.eia.gov/v2"
log = logging.getLogger(__name__)


def resolve(claim, api_key=None):
    """
    Resolves energy-related claims against the EIA v2 API.
    Requires EIA_API_KEY environment variable (free at eia.gov/opendata/register.php).
    """
    api_key = api_key or os.getenv("EIA_API_KEY")
    if not api_key:
        log.debug("EIA_API_KEY not set; skipping EIA resolution")
        return {"found": False}

    keywords = _extract_keywords(claim)
    if not keywords:
        return {"found": False}

    try:
        url = f"{EIA_BASE}/seriesid/{'+'.join(keywords[:2])}"
        resp = requests.get(
            url, params={"api_key": api_key}, timeout=15, headers=DEFAULT_HEADERS
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "found": True,
                "url": url,
                "summary": str(data)[:500],
                "content": str(data),
            }
        log.debug("EIA resolve HTTP %s", resp.status_code)
    except Exception as e:
        log.debug(f"EIA resolve failed: {e}")

    return {"found": False}


def _extract_keywords(claim):
    energy_terms = [
        "electricity",
        "natural gas",
        "petroleum",
        "coal",
        "renewable",
        "kwh",
        "mwh",
        "btu",
        "barrel",
        "mcf",
    ]
    return [t for t in energy_terms if t.lower() in claim.lower()]
