#!/usr/bin/env python3
"""
Fetch NY-12 congressional district boundary from Census TIGERweb REST API.

Tries the 119th Congress field first (CD119FP), falls back to 118th (CD118FP).

Usage:
    python3 scripts/fetch_district.py
"""

import json, sys, urllib.request, urllib.parse
from pathlib import Path

OUTPUT = Path("public/ny12.geojson")

BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/0/query"


def fetch_district(cd_field: str) -> dict | None:
    params = urllib.parse.urlencode({
        "where": f"{cd_field}='12' AND STATE='36'",
        "outFields": f"{cd_field},STATE,NAME",
        "returnGeometry": "true",
        "f": "geojson",
    })
    url = f"{BASE}?{params}"
    print(f"  Trying {cd_field}: {url[:80]}…")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ny12demographics/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        features = data.get("features", [])
        if features:
            return data
        return None
    except Exception as e:
        print(f"  Failed: {e}")
        return None


def main() -> None:
    print("Fetching NY-12 district boundary from Census TIGERweb…")
    data = fetch_district("CD119")

    if not data:
        sys.exit(
            "Could not fetch NY-12 boundary from either CD119FP or CD118FP.\n"
            "Manually download from:\n"
            "  https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/0\n"
            "and save as public/ny12.geojson"
        )

    n = len(data.get("features", []))
    print(f"Got {n} feature(s).")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data))
    print(f"Written to {OUTPUT}")


if __name__ == "__main__":
    main()
