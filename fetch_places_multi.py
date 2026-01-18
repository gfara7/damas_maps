#!/usr/bin/env python3
"""
Aggregate places/POIs for Damascus (or any bbox) from multiple sources:
- OpenStreetMap / Overpass (reuses fetch_shops.py)
- Foursquare Places (requires API key)
- Overture Places (public S3 via DuckDB; optional)
- AllThePlaces export (local NDJSON/NDJSON.gz; optional)

Outputs CSV or GeoJSON compatible with /api/shops (fields: name, lat, lon, category...).

Examples:
  python fetch_places_multi.py --outfile shops_multi.geojson
  FOURSQUARE_API_KEY=... python fetch_places_multi.py --include osm,foursquare --outfile shops_multi.csv
  python fetch_places_multi.py --include osm,overture --overture-release 2024-09-18.0 --limit 5000
  python fetch_places_multi.py --include osm,alltheplaces --alltheplaces data/alltheplaces.ndjson.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import requests

try:
    import pandas as pd
except ImportError as exc:  # pragma: no cover - env guard
    raise SystemExit("pandas is required. pip install pandas") from exc

# Default bbox roughly covering Damascus
DEFAULT_BBOX = (33.35, 36.15, 33.65, 36.45)  # south, west, north, east

try:
    from fetch_shops import build_overpass_query, call_overpass, extract_points
except Exception as exc:  # pragma: no cover - import guard
    raise SystemExit(f"Unable to import fetch_shops helpers: {exc}")


def _bbox_wkt(bbox: Tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    return (
        f"POLYGON(({west} {south}, {east} {south}, {east} {north}, "
        f"{west} {north}, {west} {south}))"
    )


def _grid_centers(bbox: Tuple[float, float, float, float], nx: int = 2, ny: int = 2) -> List[Tuple[float, float]]:
    """Return evenly spaced centers over a bbox for Foursquare coverage."""
    s, w, n, e = bbox
    lats = [s + (i + 0.5) * (n - s) / ny for i in range(ny)]
    lons = [w + (j + 0.5) * (e - w) / nx for j in range(nx)]
    return [(lat, lon) for lat in lats for lon in lons]


def dedupe_places(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in rows:
        try:
            lat_val = round(float(r.get("lat", 0.0)), 6)
            lon_val = round(float(r.get("lon", 0.0)), 6)
        except Exception:
            continue
        name_val = str(r.get("name") or "").strip().lower()
        key = (lat_val, lon_val, name_val)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


def normalize_osm(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    norm: List[Dict[str, Any]] = []
    for r in points:
        norm.append(
            {
                "source": "osm",
                "id": f"osm:{r.get('osm_type')}:{r.get('osm_id')}",
                "name": r.get("name") or r.get("brand") or "",
                "category": r.get("category"),
                "lat": r.get("lat"),
                "lon": r.get("lon"),
                "addr_street": r.get("addr_street"),
                "addr_housenumber": r.get("addr_housenumber"),
                "addr_city": r.get("addr_city"),
                "phone": r.get("phone"),
                "website": r.get("website"),
                "opening_hours": r.get("opening_hours"),
            }
        )
    return norm


def fetch_osm(bbox: Tuple[float, float, float, float], area: str, amenities: Sequence[str], tags: Sequence[str]) -> List[Dict[str, Any]]:
    shop_regex = "|".join(tags)
    ql = build_overpass_query(area, bbox, shop_regex, list(amenities))
    data = call_overpass(ql)
    points = extract_points(data)
    return normalize_osm(points)


def fetch_foursquare(
    bbox: Tuple[float, float, float, float],
    api_key: str,
    categories: Sequence[str],
    radius_m: int = 9000,
    limit_per_cell: int = 50,
    grid: Tuple[int, int] = (2, 2),
) -> List[Dict[str, Any]]:
    if not api_key:
        print("Skipping Foursquare: no API key provided (set FOURSQUARE_API_KEY).")
        return []

    api_version = os.environ.get("FOURSQUARE_API_VERSION", "2025-06-17")
    token = api_key if api_key.strip().lower().startswith("bearer ") else f"Bearer {api_key}"
    headers = {
        "Authorization": token,
        "Accept": "application/json",
        "X-Places-Api-Version": api_version,
    }
    # New host per migration guide
    url = "https://places-api.foursquare.com/places/search"
    centers = _grid_centers(bbox, nx=grid[0], ny=grid[1])
    rows: List[Dict[str, Any]] = []
    seen_ids = set()
    cats = ",".join(categories)

    for lat, lon in centers:
        cursor = None
        fetched = 0
        while True:
            params = {
                "ll": f"{lat},{lon}",
                "radius": radius_m,
                "categories": cats,
                "limit": min(limit_per_cell, 50),
            }
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(url, headers=headers, params=params, timeout=12)
            if resp.status_code == 429:
                raise SystemExit("Foursquare rate-limited (429). Try later or reduce calls.")
            if resp.status_code >= 400:
                detail = (resp.text or "").strip()
                raise SystemExit(
                    f"Foursquare error {resp.status_code}: {detail[:400] or 'no body'} | params={params}"
                )
            data = resp.json()
            for place in data.get("results", []):
                fsq_id = place.get("fsq_place_id") or place.get("fsq_id")
                if not fsq_id or fsq_id in seen_ids:
                    continue
                seen_ids.add(fsq_id)
                loc = place.get("location") or {}
                # New API exposes latitude/longitude directly; fall back to location/geocodes for older responses.
                lat_val = place.get("latitude") or loc.get("latitude")
                lon_val = place.get("longitude") or loc.get("longitude")
                if (lat_val is None or lon_val is None) and place.get("geocodes"):
                    main = (place.get("geocodes") or {}).get("main") or {}
                    lat_val = lat_val or main.get("latitude")
                    lon_val = lon_val or main.get("longitude")
                cats_list = place.get("categories") or []
                cat_name = cats_list[0].get("name") if cats_list else None
                rows.append(
                    {
                        "source": "foursquare",
                        "id": f"fsq:{fsq_id}",
                        "name": place.get("name") or "",
                        "category": cat_name,
                        "lat": lat_val,
                        "lon": lon_val,
                        "addr_street": loc.get("address") or loc.get("street"),
                        "addr_city": loc.get("locality"),
                        "phone": (place.get("tel") or place.get("telephone")),
                        "website": place.get("website"),
                    }
                )
                fetched += 1
                if fetched >= limit_per_cell:
                    break
            if fetched >= limit_per_cell:
                break
            cursor = data.get("context", {}).get("next_cursor")
            if not cursor:
                break
    return rows


def fetch_overture(bbox: Tuple[float, float, float, float], release: str, limit: int) -> List[Dict[str, Any]]:
    try:
        import duckdb  # type: ignore
    except ImportError:
        print("Skipping Overture: duckdb not installed (pip install duckdb).")
        return []

    wkt = _bbox_wkt(bbox)
    sources = [
        ("s3", f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"),
        ("s3-https", f"https://overturemaps-us-west-2.s3.amazonaws.com/release/{release}/theme=places/type=place/*"),
        ("azure-dfs", f"https://overturemapswestus2.dfs.core.windows.net/release/{release}/theme=places/type=place/*"),
        ("azure-blob", f"https://overturemapswestus2.blob.core.windows.net/release/{release}/theme=places/type=place/*"),
    ]

    last_exc: Exception | None = None
    for label, path in sources:
        sql = f"""
        INSTALL httpfs;
        LOAD httpfs;
        INSTALL spatial;
        LOAD spatial;
        SET s3_region='us-west-2';
        SET s3_endpoint='s3.amazonaws.com';
        SET s3_url_style='path';
        SET s3_use_ssl=true;
        SET s3_access_key_id='';
        SET s3_secret_access_key='';
        SET s3_session_token='';
        SELECT
          id,
          COALESCE(names['primary'].value, names['common'].value, names['en'].value, '') AS name,
          COALESCE(list_element(categories, 1), '') AS category,
          ST_Y(geometry) AS lat,
          ST_X(geometry) AS lon,
          CASE
            WHEN array_length(addresses) > 0 THEN addresses[1].street
            ELSE NULL
          END AS addr_street,
          CASE
            WHEN array_length(addresses) > 0 THEN addresses[1].city
            ELSE NULL
          END AS addr_city,
          CASE
            WHEN array_length(websites) > 0 THEN websites[1]
            ELSE NULL
          END AS website,
          CASE
            WHEN array_length(phones) > 0 THEN phones[1]
            ELSE NULL
          END AS phone
        FROM read_parquet('{path}', filename=true, hive_partitioning=1)
        WHERE ST_Intersects(geometry, ST_GeomFromText('{wkt}', 4326))
        LIMIT {limit};
        """
        try:
            con = duckdb.connect()
            res = con.execute(sql).fetchall()
            cols = [c[0] for c in con.description]
            con.close()
            break
        except Exception as exc:
            last_exc = exc
            try:
                con.close()
            except Exception:
                pass
            continue
    else:
        print(f"Warning: Overture query failed: {last_exc}")
        return []

    idx = {name: i for i, name in enumerate(cols)}
    rows: List[Dict[str, Any]] = []
    for row in res:
        rows.append(
            {
                "source": "overture",
                "id": f"overture:{row[idx['id']]}",
                "name": row[idx.get("name")],
                "category": row[idx.get("category")],
                "lat": row[idx.get("lat")],
                "lon": row[idx.get("lon")],
                "addr_street": row[idx.get("addr_street")],
                "addr_city": row[idx.get("addr_city")],
                "phone": row[idx.get("phone")],
                "website": row[idx.get("website")],
            }
        )
    return rows


def fetch_alltheplaces(path: Path, bbox: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    if not path.exists():
        print(f"Skipping AllThePlaces: {path} does not exist.")
        return []

    opener = gzip.open if path.suffix == ".gz" else open
    s, w, n, e = bbox
    rows: List[Dict[str, Any]] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            geom = obj.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue
            lon, lat = coords[:2]
            if not (s <= lat <= n and w <= lon <= e):
                continue
            props = obj.get("properties") or {}
            rows.append(
                {
                    "source": "alltheplaces",
                    "id": props.get("id") or props.get("@id") or "",
                    "name": props.get("name") or props.get("brand") or "",
                    "category": props.get("category") or props.get("type"),
                    "lat": lat,
                    "lon": lon,
                    "addr_street": props.get("addr:street") or props.get("street"),
                    "addr_city": props.get("addr:city") or props.get("city"),
                    "phone": props.get("phone"),
                    "website": props.get("website"),
                }
            )
    return rows


def _read_existing(outfile: Path, outfmt: str) -> List[Dict[str, Any]]:
    if not outfile.exists():
        return []
    if outfmt == "csv":
        try:
            df = pd.read_csv(outfile, encoding="utf-8-sig")
            return df.to_dict(orient="records")
        except Exception:
            print(f"Warning: failed to read existing CSV {outfile}; ignoring its contents.")
            return []
    try:
        data = json.loads(outfile.read_text(encoding="utf-8"))
        feats = data.get("features", [])
        rows = []
        for f in feats:
            props = f.get("properties", {}) or {}
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                props = dict(props)
                props["lon"] = coords[0]
                props["lat"] = coords[1]
                rows.append(props)
        return rows
    except Exception:
        print(f"Warning: failed to read existing GeoJSON {outfile}; ignoring its contents.")
        return []


def write_outputs(rows: List[Dict[str, Any]], outfmt: str, outfile: Path, mode: str) -> None:
    outfmt = outfmt.lower()
    mode = mode.lower()
    existing: List[Dict[str, Any]] = []
    if outfile.exists():
        if mode == "fail":
            raise SystemExit(f"{outfile} exists. Use --mode overwrite or append.")
        existing = _read_existing(outfile, outfmt)

    combined = dedupe_places(existing + rows) if mode in ("append", "merge", "dedupe") else rows

    # Write atomically to avoid partial files
    tmp = outfile.with_suffix(outfile.suffix + ".tmp")
    if outfmt == "csv":
        df = pd.DataFrame(combined)
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
    else:
        features = []
        for r in combined:
            props = dict(r)
            lat = props.pop("lat", None)
            lon = props.pop("lon", None)
            if lat is None or lon is None:
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": props,
                }
            )
        gj = {"type": "FeatureCollection", "features": features}
        tmp.write_text(json.dumps(gj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(outfile)
    print(f"Wrote {outfile} ({len(combined)} rows, mode={mode})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch Damascus places from multiple sources.")
    p.add_argument(
        "--bbox",
        default=None,
        help="Bounding box 'south,west,north,east' (default: Damascus area)",
    )
    p.add_argument("--area", default="Damascus", help="Area name for Overpass (fallback if no bbox)")
    p.add_argument(
        "--include",
        default="osm",
        help="Comma-separated sources to include: osm,foursquare,overture,alltheplaces",
    )
    p.add_argument("--out", choices=["csv", "geojson"], default="geojson", help="Output format")
    p.add_argument("--outfile", default="shops_multi.geojson", help="Output file name")
    p.add_argument("--amenities", default="supermarket,cafe,restaurant", help="Amenity list for OSM (comma separated)")
    p.add_argument(
        "--tags",
        default="supermarket,grocery,convenience,department_store,coffee_shop,bakery",
        help="Shop tags regex list for OSM (comma separated)",
    )
    p.add_argument("--foursquare-radius", type=int, default=9000, help="Radius (m) per grid center")
    p.add_argument("--foursquare-categories", default="13000,13001,13002,13003,13099,19014,19016", help="Foursquare category IDs")
    p.add_argument(
        "--foursquare-grid",
        default="2,2",
        help="Grid subdivisions as 'nx,ny' to cover the bbox with multiple calls (default: 2,2)",
    )
    p.add_argument("--overture-release", default="2025-12-17.0", help="Overture release ID")
    p.add_argument("--limit", type=int, default=5000, help="Row limit per source where applicable")
    p.add_argument("--alltheplaces", type=Path, default=None, help="Path to AllThePlaces NDJSON/NDJSON.gz export")
    p.add_argument(
        "--mode",
        choices=["append", "overwrite", "fail"],
        default="append",
        help="Write mode: append (dedupe by lat/lon/name), overwrite, or fail if file exists (default: append)",
    )
    return p.parse_args()


def parse_bbox_arg(raw: str | None) -> Tuple[float, float, float, float]:
    if not raw:
        return DEFAULT_BBOX
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise SystemExit("--bbox must be 'south,west,north,east'")
    try:
        return tuple(map(float, parts))  # type: ignore[return-value]
    except ValueError as exc:  # pragma: no cover - CLI guard
        raise SystemExit(f"Invalid bbox values: {exc}") from exc


def parse_grid_arg(raw: str) -> Tuple[int, int]:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        raise SystemExit("--foursquare-grid must be 'nx,ny'")
    try:
        nx, ny = map(int, parts)
    except ValueError as exc:  # pragma: no cover - CLI guard
        raise SystemExit(f"Invalid grid values: {exc}") from exc
    if nx < 1 or ny < 1:
        raise SystemExit("--foursquare-grid values must be >= 1")
    return nx, ny


def main() -> None:
    args = parse_args()
    bbox = parse_bbox_arg(args.bbox)
    fsq_grid = parse_grid_arg(args.foursquare_grid)
    include = {s.strip().lower() for s in args.include.split(",") if s.strip()}
    outfile = Path(args.outfile)
    all_rows: List[Dict[str, Any]] = []

    if "osm" in include:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
        amenities = [a.strip() for a in args.amenities.split(",") if a.strip()]
        print(f"Fetching OSM/Overpass for bbox {bbox} ...")
        all_rows.extend(fetch_osm(bbox, args.area, amenities, tags))

    if "foursquare" in include:
        api_key = os.environ.get("FOURSQUARE_API_KEY", "")
        cats = [c.strip() for c in args.foursquare_categories.split(",") if c.strip()]
        print(f"Fetching Foursquare ({len(cats)} categories) ...")
        all_rows.extend(
            fetch_foursquare(
                bbox,
                api_key,
                cats,
                radius_m=args.foursquare_radius,
                limit_per_cell=args.limit,
                grid=fsq_grid,
            )
        )

    if "overture" in include:
        print(f"Fetching Overture release {args.overture_release} (limit {args.limit}) ...")
        all_rows.extend(fetch_overture(bbox, args.overture_release, limit=args.limit))

    if "alltheplaces" in include and args.alltheplaces:
        print(f"Filtering AllThePlaces from {args.alltheplaces} ...")
        all_rows.extend(fetch_alltheplaces(args.alltheplaces, bbox))
    elif "alltheplaces" in include:
        print("Skipping AllThePlaces: provide --alltheplaces path.")

    all_rows = dedupe_places(all_rows)
    write_outputs(all_rows, args.out, outfile, args.mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:  # pragma: no cover - CLI guard
        raise SystemExit(130)
