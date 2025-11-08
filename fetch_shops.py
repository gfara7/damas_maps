#!/usr/bin/env python3
"""
Fetch supermarket/shop POIs for Damascus (or any area) from OpenStreetMap via Overpass.

Features:
- Queries amenity=supermarket and common shop=* categories (configurable)
- Area search by administrative area name (Damascus / Dimashq) or a bbox
- Outputs GeoJSON or CSV, optional quick Leaflet map (HTML) via folium

Usage examples:
  python fetch_shops.py                          # GeoJSON for Damascus
  python fetch_shops.py --out csv --outfile shops.csv
  python fetch_shops.py --bbox "33.35,36.15,33.65,36.45" --map shops_map.html
  python fetch_shops.py --tags "supermarket,grocery,convenience,department_store"

Note: Be considerate of Overpass rate limits. This script issues a single query
by default and sets a User-Agent string; avoid frequent repeated calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def build_overpass_query(
    area_name: str,
    bbox: Optional[Tuple[float, float, float, float]],
    shop_regex: str,
    amenities: List[str],
) -> str:
    header = "[out:json][timeout:120];\n"
    if bbox:
        s, w, n, e = bbox
        amenity_filters = "\n".join(
            [
                f"  node[\"amenity\"=\"{a}\"]({s},{w},{n},{e});\n  way[\"amenity\"=\"{a}\"]({s},{w},{n},{e});\n  relation[\"amenity\"=\"{a}\"]({s},{w},{n},{e});"
                for a in amenities
            ]
        )
        body = f"""
(
{amenity_filters}
  node["shop"~"{shop_regex}"]({s},{w},{n},{e});
  way["shop"~"{shop_regex}"]({s},{w},{n},{e});
  relation["shop"~"{shop_regex}"]({s},{w},{n},{e});
);
out center qt;
""".strip()
        return header + body + "\n"

    # Area search: try English name and local spelling
    # We union multiple matching admin areas to be robust.
    area_block = f"""
(
  area["boundary"="administrative"]["name:en"="{area_name}"];
  area["boundary"="administrative"]["name"="{area_name}"];
  area["boundary"="administrative"]["name"="Dimashq"];
)->.searchArea;
"""

    amenity_filters = "\n".join(
        [
            f"  node[\"amenity\"=\"{a}\"](area.searchArea);\n  way[\"amenity\"=\"{a}\"](area.searchArea);\n  relation[\"amenity\"=\"{a}\"](area.searchArea);"
            for a in amenities
        ]
    )

    body = f"""
{area_block}
(
{amenity_filters}
  node["shop"~"{shop_regex}"](area.searchArea);
  way["shop"~"{shop_regex}"](area.searchArea);
  relation["shop"~"{shop_regex}"](area.searchArea);
);
out center qt;
""".strip()
    return header + body + "\n"


def call_overpass(ql: str) -> Dict[str, Any]:
    headers = {"User-Agent": "damas-maps-fetch/1.0 (contact: local)"}
    last_exc: Optional[Exception] = None
    for url in OVERPASS_ENDPOINTS:
        try:
            resp = requests.post(url, data=ql.encode("utf-8"), headers=headers, timeout=180)
            if resp.status_code == 429:
                last_exc = RuntimeError(f"Overpass rate limited at {url} (429)")
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            continue
    raise SystemExit(f"All Overpass endpoints failed. Last error: {last_exc}")


def extract_points(osm_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    elements = osm_json.get("elements", [])
    rows: List[Dict[str, Any]] = []
    for el in elements:
        etype = el.get("type")
        tags = el.get("tags", {})
        if etype == "node":
            lat = el.get("lat")
            lon = el.get("lon")
        else:
            center = el.get("center") or {}
            lat = center.get("lat")
            lon = center.get("lon")
        if lat is None or lon is None:
            continue

        name = tags.get("name:en") or tags.get("name") or tags.get("brand") or ""
        shop = tags.get("shop")
        amenity = tags.get("amenity")
        brand = tags.get("brand")
        street = tags.get("addr:street")
        housenumber = tags.get("addr:housenumber")
        city = tags.get("addr:city")
        phone = tags.get("phone") or tags.get("contact:phone")
        website = tags.get("website") or tags.get("contact:website")
        opening_hours = tags.get("opening_hours")

        rows.append(
            {
                "osm_type": etype,
                "osm_id": el.get("id"),
                "name": name,
                "category": amenity or shop,
                "source_tag": ("amenity" if amenity else ("shop" if shop else None)),
                "brand": brand,
                "lat": lat,
                "lon": lon,
                "addr_street": street,
                "addr_housenumber": housenumber,
                "addr_city": city,
                "phone": phone,
                "website": website,
                "opening_hours": opening_hours,
            }
        )
    # Deduplicate exact lat,lon,name triples
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for r in rows:
        key = (round(r["lat"], 7), round(r["lon"], 7), (r["name"] or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


def to_geojson(points: List[Dict[str, Any]]) -> Dict[str, Any]:
    features = []
    for r in points:
        props = r.copy()
        lat = props.pop("lat")
        lon = props.pop("lon")
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_outputs(points: List[Dict[str, Any]], outfmt: str, outfile: Path, map_html: Optional[Path]):
    outfmt = outfmt.lower()
    if outfmt == "csv":
        df = pd.DataFrame(points)
        df.to_csv(outfile, index=False, encoding="utf-8-sig")
        print(f"Wrote {outfile}")
    else:
        gj = to_geojson(points)
        outfile.write_text(json.dumps(gj, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {outfile}")

    if map_html:
        try:
            import folium  # type: ignore

            if not points:
                print("No points to map; skipping folium map")
                return
            lat0 = points[0]["lat"]
            lon0 = points[0]["lon"]
            m = folium.Map(location=[lat0, lon0], zoom_start=12)
            for r in points:
                folium.CircleMarker(
                    location=[r["lat"], r["lon"]],
                    radius=4,
                    tooltip=r.get("name") or r.get("brand") or r.get("category") or "Shop",
                    popup=(
                        f"<b>{(r.get('name') or r.get('brand') or 'Shop')}</b><br>"
                        f"{r.get('category') or ''}<br>"
                        f"{(r.get('addr_street') or '')} {(r.get('addr_housenumber') or '')}"
                    ),
                    color="#d97706",
                    fillColor="#d97706",
                    fillOpacity=0.8,
                    weight=2,
                ).add_to(m)
            m.save(str(map_html))
            print(f"Wrote {map_html}")
        except Exception as exc:
            print(f"Warning: failed to write folium map: {exc}")


def parse_bbox(s: str) -> Tuple[float, float, float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--bbox must be 'south,west,north,east'")
    try:
        south, west, north, east = map(float, parts)
    except ValueError:
        raise argparse.ArgumentTypeError("--bbox values must be floats")
    return south, west, north, east


def main():
    p = argparse.ArgumentParser(description="Fetch amenities/shops POIs from OSM Overpass")
    p.add_argument("--area", default="Damascus", help="Administrative area name (default: Damascus)")
    p.add_argument(
        "--bbox",
        type=parse_bbox,
        default=None,
        help="Bounding box 'south,west,north,east' to use instead of area search",
    )
    p.add_argument(
        "--amenities",
        default="supermarket,cafe,restaurant",
        help="Comma-separated amenity=* values to include",
    )
    p.add_argument(
        "--tags",
        default="supermarket,grocery,convenience,department_store,mini_market,minimarket,coffee,coffee_shop,bakery",
        help="Comma-separated shop=* values to include (regex OR)",
    )
    p.add_argument("--out", choices=["geojson", "csv"], default="geojson", help="Output format")
    p.add_argument("--outfile", default="damascus_shops.geojson", help="Output filename")
    p.add_argument("--map", dest="map_html", default=None, help="Optional Folium map HTML output path")
    args = p.parse_args()

    shop_vals = "|".join(v.strip() for v in args.tags.split(",") if v.strip())
    amenities = [v.strip() for v in args.amenities.split(",") if v.strip()]
    ql = build_overpass_query(args.area, args.bbox, shop_vals, amenities)
    data = call_overpass(ql)
    points = extract_points(data)
    if not points:
        print("No results found.")
    write_outputs(points, args.out, Path(args.outfile), Path(args.map_html) if args.map_html else None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
