#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from solve_vrp import (
    Stop,
    Vehicle,
    build_data_model,
    solve_vrp,
    get_routes,
    to_geojson,
    export_assignments_excel,
    export_routes_kml,
)


def parse_args():
    p = argparse.ArgumentParser(description="Plan a daily delivery batch from JSON input")
    p.add_argument("--input", "-i", required=True, help="Path to daily batch JSON file")
    p.add_argument("--trucks", type=int, default=None, help="If vehicles not provided, number of trucks to create")
    p.add_argument("--capacity", type=int, default=None, help="If vehicles not provided, capacity per truck")
    p.add_argument("--prefix", default="daily", help="Filename prefix for outputs (default: daily)")
    return p.parse_args()


def build_from_json(obj: Dict[str, Any], trucks: Optional[int], capacity: Optional[int]):
    # Depot
    depot = obj.get("depot") or {}
    try:
        dep_name = depot.get("name", "Depot")
        dep_lat = float(depot["lat"])  # required
        dep_lon = float(depot["lon"])  # required
    except (KeyError, ValueError, TypeError):
        raise SystemExit("Input JSON must contain depot.lat and depot.lon")

    dep_tw = None
    if depot.get("tw_start") is not None and depot.get("tw_end") is not None:
        dep_tw = (int(depot["tw_start"]), int(depot["tw_end"]))

    stops: List[Stop] = [
        Stop(dep_name, dep_lat, dep_lon, demand=0, service_min=int(depot.get("service_min", 0)), tw=dep_tw)
    ]

    # Delivery stops
    raw_stops = obj.get("stops") or []
    if not isinstance(raw_stops, list) or len(raw_stops) == 0:
        raise SystemExit("Input JSON must contain a non-empty 'stops' array")

    for s in raw_stops:
        name = (s.get("name") or "").strip() or "Stop"
        try:
            lat = float(s["lat"])  # required
            lon = float(s["lon"])  # required
        except (KeyError, ValueError, TypeError):
            raise SystemExit(f"Stop '{name}' missing/invalid lat/lon")
        demand = int(s.get("demand", 1))
        service_min = int(s.get("service_min", 5))
        tw = None
        if s.get("tw_start") is not None and s.get("tw_end") is not None:
            tw = (int(s["tw_start"]), int(s["tw_end"]))
        stops.append(Stop(name, lat, lon, demand=max(demand, 0), service_min=max(service_min, 0), tw=tw))

    total_demand = sum(s.demand for s in stops)

    # Vehicles
    vehicles: List[Vehicle] = []
    if isinstance(obj.get("vehicles"), list) and obj["vehicles"]:
        for v in obj["vehicles"]:
            name = (v.get("name") or "").strip() or f"Van {len(vehicles)+1}"
            cap = int(v.get("capacity", 1))
            max_min = v.get("max_route_min")
            vehicles.append(
                Vehicle(name=name, capacity=cap, start_index=0, end_index=None,
                        max_route_min=int(max_min) if max_min else None)
            )
    else:
        # Auto-derive vehicles if not provided
        if trucks is None or capacity is None:
            raise SystemExit("Provide vehicles in JSON or pass --trucks and --capacity")
        for i in range(trucks):
            vehicles.append(Vehicle(name=f"Van {i+1}", capacity=capacity, start_index=0))

    if sum(v.capacity for v in vehicles) < total_demand:
        print("Warning: total vehicle capacity is less than total demand; routes may be infeasible")

    return stops, vehicles


def main():
    args = parse_args()
    in_path = Path(args.input)
    obj = json.loads(in_path.read_text(encoding="utf-8"))

    stops, vehicles = build_from_json(obj, args.trucks, args.capacity)

    # Build/solve
    data = build_data_model(stops, vehicles)
    routing, solution, time_dim, manager = solve_vrp(data)
    if solution is None:
        raise SystemExit("No solution found for this batch")

    routes = get_routes(routing, solution, time_dim, data, manager)

    # Outputs with custom prefix
    prefix = args.prefix.rstrip("._-") or "daily"
    gj = to_geojson(routes, data)
    geo_path = f"{prefix}_routes.geojson"
    Path(geo_path).write_text(json.dumps(gj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {geo_path}")

    # Excel and KML (reusing helpers from solve_vrp)
    export_assignments_excel(routes, data, data["duration_matrix_min"], data["distance_matrix_m"], outfile=f"{prefix}_assignments.xlsx")
    export_routes_kml(gj, routes, data, folder=f"{prefix}_kml")

    # Optional HTML map
    try:
        from solve_vrp import quick_map

        quick_map(gj, f"{prefix}_map.html")
    except Exception:
        pass


if __name__ == "__main__":
    main()

