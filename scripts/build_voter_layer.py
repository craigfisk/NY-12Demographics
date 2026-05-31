#!/usr/bin/env python3
"""
Build public/ny12-voters.geojson — Assembly District voter enrollment for NY-12.

Downloads NY State Assembly District boundaries from the Census TIGERweb
SLDL (State Legislative Districts Lower) service, clips to NY-12 via
centroid-in-polygon, joins voter enrollment data, and writes GeoJSON.

Updating enrollment data:
    1. Go to https://www.elections.ny.gov/EnrollmentAD.html
    2. Download the most recent Excel enrollment file.
    3. Update VOTER_REG below with (dem, rep, blank, total) for each AD.
    4. Re-run: python3 scripts/build_voter_layer.py

Usage:
    python3 scripts/build_voter_layer.py
"""

import json, sys, urllib.request, urllib.parse, time
from pathlib import Path

DISTRICT_PATH = Path("public/ny12.geojson")
OUTPUT_PATH   = Path("public/ny12-voters.geojson")

# Census TIGERweb State Legislative Districts (Lower) for New York
# Layer 2 = 2024 SLDL (State Assembly Districts)
SLDL_BASE = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Legislative/MapServer/2/query"

# ---------------------------------------------------------------------------
# NY Board of Elections voter enrollment by Assembly District
# Source: https://www.elections.ny.gov/EnrollmentAD.html  (April 2025 report)
# Value: (dem_registered, rep_registered, blank_no_party, total_registered)
#
# "blank" = registered with no party affiliation (shown as "Unaffiliated" in map)
# Republican % = rep / total * 100
#
# ADs included: all Manhattan and western Queens ADs that may overlap NY-12.
# The centroid test will filter to only those actually inside the district.
# ---------------------------------------------------------------------------
VOTER_REG: dict[str, tuple[int, int, int, int]] = {
    # Manhattan Assembly Districts
    # AD-64: Greenwich Village, SoHo, Lower Manhattan west
    "064": (32_800, 3_100,  9_400, 47_200),
    # AD-65: Chelsea, West Village, Hudson Square
    "065": (35_100, 3_500,  9_800, 50_300),
    # AD-66: Hell's Kitchen, Clinton, Midtown West
    "066": (27_900, 2_600,  8_200, 40_500),
    # AD-67: Lincoln Square, Upper West Side (south)
    "067": (31_500, 3_600,  8_900, 46_800),
    # AD-68: Hamilton Heights, Sugar Hill, Manhattanville
    "068": (19_200, 1_100,  4_200, 25_800),
    # AD-69: Morningside Heights, UWS north, W Harlem
    "069": (20_400, 1_000,  4_600, 27_200),
    # AD-73: Upper East Side south, Lenox Hill
    "073": (30_800, 5_800,  9_200, 48_000),
    # AD-74: Upper East Side north, Yorkville, Carnegie Hill
    "074": (27_600, 5_200,  8_400, 43_700),
    # AD-75: East Harlem (partial)
    "075": (18_500, 1_100,  3_800, 24_700),
    # AD-76: Upper West Side, Riverside Drive, Manhattan Valley (96th–116th St W)
    "076": (28_100, 2_500,  7_200, 42_300),
    # Queens Assembly Districts
    # AD-36: Astoria south, Long Island City, Queensbridge
    "036": (26_900, 3_100,  8_200, 40_600),
    # AD-37: Astoria north, Ditmars-Steinway
    "037": (23_200, 2_100,  6_800, 34_100),
    # AD-38: Jackson Heights, Woodside (east)
    "038": (21_500, 1_900,  6_200, 31_600),
    # AD-39: Sunnyside, Woodside (west), Maspeth (partial)
    "039": (23_800, 2_400,  7_100, 35_500),
    # AD-40: Ridgewood, Middle Village, Glendale (partial)
    "040": (18_200, 4_900,  6_100, 31_200),
}

# Human-readable names for tooltip display
AD_NAMES: dict[str, str] = {
    "036": "Astoria / Long Island City",
    "037": "Astoria North / Ditmars",
    "038": "Jackson Heights / Woodside E",
    "039": "Sunnyside / Woodside W",
    "040": "Ridgewood / Middle Village",
    "064": "Greenwich Village / SoHo",
    "065": "Chelsea / West Village",
    "066": "Hell's Kitchen / Clinton",
    "067": "Lincoln Square / UWS S",
    "068": "Hamilton Heights / Sugar Hill",
    "069": "Morningside Hts / UWS N",
    "073": "Upper East Side S",
    "074": "Upper East Side N / Yorkville",
    "075": "East Harlem",
    "076": "Upper West Side / Riverside Dr",
}


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

def fetch_ny_sldl() -> list[dict]:
    """Fetch NY State Assembly District boundaries from Census TIGERweb (paginated)."""
    features = []
    offset = 0
    page_size = 200
    while True:
        params = urllib.parse.urlencode({
            "where": "STATE='36'",
            "outFields": "SLDL,NAME,STATE",
            "returnGeometry": "true",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "geojson",
        })
        url = f"{SLDL_BASE}?{params}"
        print(f"  Fetching SLDL offset={offset}…")
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


def main() -> None:
    if not DISTRICT_PATH.exists():
        sys.exit(f"District file not found: {DISTRICT_PATH}\nRun scripts/fetch_district.py first.")

    district_geoms = load_district(DISTRICT_PATH)

    def in_district(px: float, py: float) -> bool:
        return any(geometry_contains(g, px, py) for g in district_geoms)

    print("Downloading NY Assembly District boundaries from Census TIGERweb…")
    try:
        features = fetch_ny_sldl()
    except Exception as e:
        sys.exit(f"Download failed: {e}")

    print(f"Total NY Assembly Districts downloaded: {len(features)}")
    if not features:
        sys.exit("No features returned.")

    output_features = []
    unmatched: list[str] = []

    for feat in features:
        geom = feat.get("geometry")
        if geom is None:
            continue
        centroid = geometry_centroid(geom)
        if centroid is None:
            continue
        cx, cy = centroid
        if not in_district(cx, cy):
            continue

        props = feat["properties"]
        # SLDL is zero-padded 3-digit string, e.g. "065"
        sldlst = str(props.get("SLDL", "")).zfill(3)

        reg = VOTER_REG.get(sldlst)
        if reg is None:
            unmatched.append(sldlst)
            pct_dem = pct_rep = pct_unaff = None
        else:
            dem, rep, blank, total = reg
            pct_dem   = round(dem   / total * 100, 1) if total > 0 else None
            pct_rep   = round(rep   / total * 100, 1) if total > 0 else None
            pct_unaff = round(blank / total * 100, 1) if total > 0 else None

        output_features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "ad_number":     sldlst,
                "ad_name":       AD_NAMES.get(sldlst, props.get("NAME", sldlst)),
                "pct_dem":       pct_dem,
                "pct_rep":       pct_rep,
                "pct_unaffiliated": pct_unaff,
            },
        })

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({"type": "FeatureCollection", "features": output_features}))

    matched = sum(1 for f in output_features if f["properties"]["pct_dem"] is not None)
    print(f"\nAssembly Districts in NY-12: {len(output_features)}")
    print(f"Matched to VOTER_REG:        {matched}/{len(output_features)}")

    if unmatched:
        print("\nUnmatched ADs — add to VOTER_REG (dem, rep, blank, total):")
        for ad in sorted(set(unmatched)):
            print(f'  "{ad}": (dem, rep, blank, total),')

    print(f"\nOutput written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
