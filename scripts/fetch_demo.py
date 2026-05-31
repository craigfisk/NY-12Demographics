#!/usr/bin/env python3
"""
Build public/ny12-demo.geojson — census-tract demographics for NY-12.

Steps:
  1. Load ny12.geojson to get the district boundary.
  2. Fetch Census tract geometries for Manhattan (061) and Queens (081)
     from the Census TIGERweb REST API (paginated).
  3. Clip to NY-12 via centroid-in-polygon test.
  4. Fetch ACS 2023 5-year demographics from Census Reporter public API
     (no API key required) in batches of 50 tracts.
  5. Write public/ny12-demo.geojson.

Usage:
    python3 scripts/fetch_demo.py
"""

import json, sys, time, urllib.request, urllib.parse
from pathlib import Path

DISTRICT_PATH = Path("public/ny12.geojson")
OUTPUT_PATH   = Path("public/ny12-demo.geojson")

COUNTIES = ["061", "081"]   # Manhattan, Queens
STATE    = "36"

TIGER_BASE    = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer/0/query"
CR_BASE       = "https://api.censusreporter.org/1.0/data/show/latest"
CR_TABLES     = ["B03002", "C16001", "B01001"]   # fetched one at a time
CR_BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Pure-Python spatial helpers
# ---------------------------------------------------------------------------

def _ring_contains(ring: list, px: float, py: float) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > py) != (yj > py):
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def geometry_contains(geom: dict, px: float, py: float) -> bool:
    t = geom["type"]
    if t == "Polygon":
        rings = geom["coordinates"]
        if not _ring_contains(rings[0], px, py):
            return False
        return not any(_ring_contains(h, px, py) for h in rings[1:])
    if t == "MultiPolygon":
        return any(
            geometry_contains({"type": "Polygon", "coordinates": poly}, px, py)
            for poly in geom["coordinates"]
        )
    return False


def ring_centroid(ring: list) -> tuple[float, float]:
    xs = [c[0] for c in ring]
    ys = [c[1] for c in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def geometry_centroid(geom: dict) -> tuple[float, float] | None:
    t = geom["type"]
    if t == "Polygon":
        return ring_centroid(geom["coordinates"][0])
    if t == "MultiPolygon":
        largest = max(geom["coordinates"], key=lambda p: len(p[0]))
        return ring_centroid(largest[0])
    return None


def load_district(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if raw.get("type") == "FeatureCollection":
        return [f["geometry"] for f in raw["features"]]
    if raw.get("type") == "Feature":
        return [raw["geometry"]]
    return [raw]


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_tracts_for_county(county: str) -> list[dict]:
    features = []
    offset = 0
    page_size = 500
    while True:
        params = urllib.parse.urlencode({
            "where": f"STATE='{STATE}' AND COUNTY='{county}'",
            "outFields": "TRACT,COUNTY,STATE",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "geojson",
        })
        url = f"{TIGER_BASE}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "ny12demographics/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            page = json.loads(r.read())
        batch = page.get("features", [])
        features.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        time.sleep(0.3)
    return features


def fetch_census_reporter(geo_ids: list[str]) -> dict[str, dict]:
    """Fetch ACS data from Census Reporter for a batch of geo_ids (one table at a time)."""
    combined: dict[str, dict] = {}
    for table in CR_TABLES:
        params = urllib.parse.urlencode({
            "table_ids": table,
            "geo_ids":   ",".join(geo_ids),
        })
        url = f"{CR_BASE}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "ny12demographics/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
        table_data = resp.get("data", {})
        for gid, tables in table_data.items():
            if gid not in combined:
                combined[gid] = {}
            combined[gid].update(tables)
        time.sleep(0.3)
    return combined


def safe_int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def extract_demographics(cr_data: dict, geo_id: str) -> dict:
    geo = cr_data.get(geo_id, {})

    b03 = geo.get("B03002", {}).get("estimate", {})
    total_pop  = safe_int(b03.get("B03002001"))
    hisp       = safe_int(b03.get("B03002012"))
    pct_hisp   = round(hisp / total_pop * 100, 1) if total_pop > 0 else 0.0

    c16 = geo.get("C16001", {}).get("estimate", {})
    lang_univ  = safe_int(c16.get("C16001001")) or 1
    spanish    = safe_int(c16.get("C16001003"))   # Spanish speakers (all English ability)
    pct_span   = round(spanish / lang_univ * 100, 1) if lang_univ > 0 else 0.0

    b01 = geo.get("B01001", {}).get("estimate", {})
    # Male 18-19, 20, 21, 22-24, 25-29, 30-34
    # Female 18-19, 20, 21, 22-24, 25-29, 30-34
    young_cols = [
        "B01001007","B01001008","B01001009","B01001010","B01001011","B01001012",
        "B01001031","B01001032","B01001033","B01001034","B01001035","B01001036",
    ]
    young     = sum(safe_int(b01.get(c)) for c in young_cols)
    total_b01 = safe_int(b01.get("B01001001")) or total_pop or 1
    pct_young = round(young / total_b01 * 100, 1) if total_b01 > 0 else 0.0

    return {
        "total_pop":    total_pop,
        "pct_hispanic": pct_hisp,
        "pct_spanish":  pct_span,
        "pct_young":    pct_young,
    }


def main() -> None:
    if not DISTRICT_PATH.exists():
        sys.exit(f"District file not found: {DISTRICT_PATH}\nRun scripts/fetch_district.py first.")

    district_geoms = load_district(DISTRICT_PATH)

    def in_district(px: float, py: float) -> bool:
        return any(geometry_contains(g, px, py) for g in district_geoms)

    # Step 1: Get tract geometries and filter to NY-12
    tract_features: list[dict] = []
    for county in COUNTIES:
        print(f"Fetching tract geometries for county {county}…")
        tracts = fetch_tracts_for_county(county)
        print(f"  {len(tracts)} tracts downloaded")
        in_d = [
            f for f in tracts
            if (g := f.get("geometry")) and
               (c := geometry_centroid(g)) and
               in_district(c[0], c[1])
        ]
        print(f"  {len(in_d)} tracts within NY-12")
        tract_features.extend(in_d)

    if not tract_features:
        sys.exit("No census tracts found within NY-12. Check the district boundary file.")

    # Build geo_ids for Census Reporter (14000US{state}{county}{tract})
    def geo_id(feat: dict) -> str:
        p = feat["properties"]
        s = STATE.zfill(2)
        c = str(p.get("COUNTY", "")).zfill(3)
        t = str(p.get("TRACT", "")).zfill(6)
        return f"14000US{s}{c}{t}"

    geo_ids = [geo_id(f) for f in tract_features]
    id_to_feat = {geo_id(f): f for f in tract_features}

    # Step 2: Fetch ACS demographics in batches from Census Reporter
    print(f"\nFetching demographics for {len(geo_ids)} tracts via Census Reporter…")
    cr_combined: dict[str, dict] = {}
    for i in range(0, len(geo_ids), CR_BATCH_SIZE):
        batch = geo_ids[i:i + CR_BATCH_SIZE]
        print(f"  batch {i // CR_BATCH_SIZE + 1} ({len(batch)} tracts)…")
        try:
            cr_combined.update(fetch_census_reporter(batch))
        except Exception as e:
            print(f"  Warning: batch failed ({e}), demographics will be zeros for this batch")
        time.sleep(0.5)

    # Step 3: Build output features
    output_features = []
    for gid, feat in id_to_feat.items():
        p     = feat["properties"]
        tract = str(p.get("TRACT", "")).zfill(6)
        county = str(p.get("COUNTY", "")).zfill(3)
        demo  = extract_demographics(cr_combined, gid)
        output_features.append({
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "TRACT":  tract,
                "COUNTY": county,
                **demo,
            },
        })

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({"type": "FeatureCollection", "features": output_features}))
    print(f"\nTotal tracts in NY-12: {len(output_features)}")
    print(f"Written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
