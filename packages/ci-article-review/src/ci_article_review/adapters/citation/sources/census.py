"""US Census Bureau data resolver."""

import requests

from ci_core.http import DEFAULT_HEADERS
import logging
import os

CENSUS_BASE = "https://api.census.gov/data"
log = logging.getLogger(__name__)


def resolve(claim, api_key=None):
    api_key = api_key or os.getenv("CENSUS_API_KEY")
    if not api_key:
        log.debug("CENSUS_API_KEY not set; skipping Census resolution")
        return {"found": False}

    dataset = _guess_dataset(claim)
    if not dataset:
        return {"found": False}

    try:
        url = f"{CENSUS_BASE}/{dataset['year']}/{dataset['name']}?get={dataset['variable']}&for=us:1&key={api_key}"
        resp = requests.get(url, timeout=15, headers=DEFAULT_HEADERS)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "found": True,
                "url": "https://data.census.gov/",
                "summary": f"Census {dataset['name']} {dataset['year']}: {data}",
                "content": str(data),
            }
    except Exception as e:
        log.debug(f"Census resolve failed: {e}")

    return {"found": False}


def _guess_dataset(claim):
    claim_lower = claim.lower()
    if "population" in claim_lower:
        return {"year": "2023", "name": "acs/acs1", "variable": "B01003_001E"}
    if "income" in claim_lower or "median household" in claim_lower:
        return {"year": "2023", "name": "acs/acs1", "variable": "B19013_001E"}
    if "poverty" in claim_lower:
        return {"year": "2023", "name": "acs/acs1", "variable": "B17001_002E"}
    return None
