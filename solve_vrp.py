#!/usr/bin/env python3
import math
import os
import time
import json
from pathlib import Path
import requests
import polyline
import folium
import pandas as pd
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# Allow overriding OSRM base URL via environment variable for deployments
OSRM_BASE = os.getenv("OSRM_BASE", "http://localhost:5000")  # from docker compose or Azure

# -----------------------
# Domain models
# -----------------------
@dataclass
class Stop:
    name: str
    lat: float
    lon: float
    demand: int = 1
    # time windows in minutes from day start (optional)
    # e.g., (9*60, 17*60) for 9:00-17:00
    tw: Optional[Tuple[int, int]] = None
    service_min: int = 5  # minutes spent at stop

@dataclass
class Vehicle:
    name: str
    capacity: int
    start_index: int  # index in locations array
    end_index: Optional[int] = None  # if None, same as start
    max_route_min: Optional[int] = 8 * 60  # hard limit (optional)
    speed_factor: float = 1.0  # multiply travel times (traffic fudge)

# -----------------------
# Example inputs
# -----------------------
# 0th entry is DEPOT. The rest are shops.
# Replace below with your actual coordinates.
# 0th entry is DEPOT followed by 30 Damascus-area delivery points.
LOCATIONS: List[Stop] = [
    Stop("Central Warehouse", 33.5130, 36.2920, demand=0, service_min=0),
    Stop("Bab Touma Market", 33.5138, 36.3091, demand=2),
    Stop("Mezzeh 86 Residences", 33.4837, 36.2352, demand=2),
    Stop("Baramkeh Square", 33.5012, 36.2844, demand=1),
    Stop("Qassaa Commercial", 33.5175, 36.3132, demand=2),
    Stop("Abu Rummaneh Offices", 33.5159, 36.3028, demand=1),
    Stop("Kafr Sousa Business Park", 33.4865, 36.2458, demand=3),
    Stop("Malki Residences", 33.5221, 36.2913, demand=2),
    Stop("Shaalan Boutiques", 33.5166, 36.2987, demand=1),
    Stop("Mazzeh Autostrade Hub", 33.4849, 36.2614, demand=2),
    Stop("Dummar Heights Center", 33.5531, 36.2405, demand=2),
    Stop("Jaramana Main Street", 33.4850, 36.3489, demand=2),
    Stop("Bab Musalla Depot", 33.5033, 36.3002, demand=1),
    Stop("Midan Market", 33.4938, 36.3033, demand=2),
    Stop("Sarouja Bookstores", 33.5150, 36.2980, demand=1),
    Stop("Berneh Street", 33.4862, 36.3311, demand=2),
    Stop("Tishreen Park Gate", 33.5228, 36.2952, demand=1),
    Stop("Rukn al Din North", 33.5403, 36.3004, demand=2),
    Stop("Qaboun Industrial", 33.5459, 36.3388, demand=3),
    Stop("Harasta Highway", 33.5635, 36.3475, demand=2),
    Stop("Douma City Center", 33.5715, 36.4012, demand=3),
    Stop("Adra Logistics Park", 33.6017, 36.4522, demand=2),
    Stop("Sayyida Zeinab Plaza", 33.4394, 36.3625, demand=2),
    Stop("Babbila Hub", 33.4572, 36.3217, demand=1),
    Stop("Yalda Residences", 33.4550, 36.3050, demand=2),
    Stop("Qatana East", 33.4405, 36.1062, demand=2),
    Stop("Jdeidet Artouz Center", 33.4261, 36.2298, demand=2),
    Stop("Sahnaya Central", 33.4289, 36.2752, demand=2),
    Stop("Kisweh Junction", 33.3643, 36.2306, demand=3),
    Stop("Hujeira Stores", 33.4402, 36.3448, demand=2),
    Stop("Harran al Awamid Depot", 33.4935, 36.5255, demand=2),
]

VEHICLES: List[Vehicle] = [
    Vehicle("Van 1", capacity=20, start_index=0, max_route_min=10 * 60),
    Vehicle("Van 2", capacity=20, start_index=0, max_route_min=10 * 60),
    Vehicle("Van 3", capacity=20, start_index=0, max_route_min=10 * 60),
    Vehicle("Van 4", capacity=20, start_index=0, max_route_min=10 * 60),
]

# -----------------------
# OSRM helpers
# -----------------------
def osrm_table(coords: List[Tuple[float, float]],
               sources: Optional[List[int]] = None,
               destinations: Optional[List[int]] = None,
               annotations: str = "duration,distance",
               chunk: int = 100) -> Dict[str, Any]:
    """
    Call OSRM /table with optional chunking when N > ~100.
    Returns a full NxN matrix for the requested indices.
    """
    n = len(coords)
    idx_sources = list(range(n)) if sources is None else sources
    idx_dest = list(range(n)) if destinations is None else destinations

    # OSRM typically handles up to ~10k table cells; chunk prudently.
    def _one_call(src_idx: List[int], dst_idx: List[int]) -> Dict[str, Any]:
        src_str = ";".join(str(i) for i in src_idx)
        dst_str = ";".join(str(i) for i in dst_idx)
        coord_str = ";".join([f"{lon},{lat}" for (lat, lon) in coords])
        url = (f"{OSRM_BASE}/table/v1/driving/{coord_str}"
               f"?sources={src_str}&destinations={dst_str}&annotations={annotations}")
        r = requests.get(url, timeout=600)
        r.raise_for_status()
        return r.json()

    # Build by blocks
    durations = [[None]*len(idx_dest) for _ in idx_sources]
    distances = [[None]*len(idx_dest) for _ in idx_sources]

    for si in range(0, len(idx_sources), chunk):
        for di in range(0, len(idx_dest), chunk):
            s_block = idx_sources[si:si+chunk]
            d_block = idx_dest[di:di+chunk]
            resp = _one_call(s_block, d_block)
            dur = resp.get("durations", [])
            dist = resp.get("distances", [])
            # place into big matrices
            for i, s in enumerate(s_block):
                for j, d in enumerate(d_block):
                    durations[si+i][di+j] = dur[i][j]
                    distances[si+i][di+j] = dist[i][j]

    return {"durations": durations, "distances": distances}

def osrm_route_between(a: Tuple[float, float], b: Tuple[float, float]) -> Dict[str, Any]:
    """
    Call OSRM /route to get geometry and steps between two points.
    """
    coord = f"{a[1]},{a[0]};{b[1]},{b[0]}"
    url = f"{OSRM_BASE}/route/v1/driving/{coord}?overview=full&geometries=polyline"
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    return r.json()

# -----------------------
# VRP with OR-Tools
# -----------------------
def build_data_model(stops: List[Stop], vehicles: List[Vehicle]) -> Dict[str, Any]:
    coords = [(s.lat, s.lon) for s in stops]
    # Fetch matrix from OSRM (seconds/meters)
    table = osrm_table(coords)

    # Convert seconds to minutes (int) for solver
    durations_min = [[int(round((c if c is not None else 0)/60.0)) for c in row] for row in table["durations"]]
    distances_m = table["distances"]

    # Demands & service times
    demands = [s.demand for s in stops]
    service_min = [s.service_min for s in stops]

    # Time windows: default [0, 24h)
    default_tw = (0, 24*60)
    time_windows = [s.tw if s.tw else default_tw for s in stops]

    # Vehicles
    starts = [v.start_index for v in vehicles]
    ends = [v.end_index if v.end_index is not None else v.start_index for v in vehicles]
    caps = [v.capacity for v in vehicles]
    max_route_min = [v.max_route_min if v.max_route_min else 24*60 for v in vehicles]

    return {
        "duration_matrix_min": durations_min,
        "distance_matrix_m": distances_m,
        "demands": demands,
        "service_min": service_min,
        "time_windows": time_windows,
        "vehicle_capacities": caps,
        "vehicle_starts": starts,
        "vehicle_ends": ends,
        "vehicle_max_route_min": max_route_min,
        "stops": stops,
        "vehicles": vehicles,
    }

def solve_vrp(data: Dict[str, Any]):
    n = len(data["duration_matrix_min"])
    num_vehicles = len(data["vehicle_capacities"])
    starts = data["vehicle_starts"]
    ends = data["vehicle_ends"]

    # New API: manager + routing
    manager = pywrapcp.RoutingIndexManager(n, num_vehicles, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    # ---- Transit (travel time + service time) ----
    duration_matrix = data["duration_matrix_min"]
    service_min = data["service_min"]

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return duration_matrix[from_node][to_node] + service_min[from_node]

    transit_cb_idx = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb_idx)

    # ---- Capacity ----
    demands = data["demands"]

    def demand_callback(from_index):
        from_node = manager.IndexToNode(from_index)
        return demands[from_node]

    demand_cb_idx = routing.RegisterUnaryTransitCallback(demand_callback)
    routing.AddDimensionWithVehicleCapacity(
        demand_cb_idx,
        0,  # no slack
        data["vehicle_capacities"],
        True,  # start cumul at zero
        "Capacity",
    )

    # ---- Time Windows ----
    time_cb_idx = transit_cb_idx
    horizon = 24 * 60
    routing.AddDimension(
        time_cb_idx,
        horizon,  # allow waiting up to full horizon
        horizon,  # max
        False,    # don't force start at 0, we'll set windows
        "Time",
    )

    time_dim = routing.GetDimensionOrDie("Time")
    time_windows = data["time_windows"]
    # Set per-node time windows
    for node, (open_t, close_t) in enumerate(time_windows):
        index = manager.NodeToIndex(node)
        time_dim.CumulVar(index).SetRange(open_t, close_t)
    # Also apply depot windows to each vehicle's start and end indices
    for v in range(num_vehicles):
        start_index = routing.Start(v)
        end_index = routing.End(v)
        s_node = starts[v]
        e_node = ends[v]
        s_open, s_close = time_windows[s_node]
        e_open, e_close = time_windows[e_node]
        time_dim.CumulVar(start_index).SetRange(s_open, s_close)
        time_dim.CumulVar(end_index).SetRange(e_open, e_close)

    # Optional: max route duration per vehicle (relative to start time)
    for v, limit in enumerate(data["vehicle_max_route_min"]):
        start_index = routing.Start(v)
        end_index = routing.End(v)
        routing.solver().Add(time_dim.CumulVar(end_index) <= time_dim.CumulVar(start_index) + limit)

    # ---- First solution strategy & local search ----
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_params.time_limit.seconds = 20  # bump if you have more stops
    search_params.log_search = True

    solution = routing.SolveWithParameters(search_params)
    return routing, solution, time_dim, manager

# -----------------------
# Extract routes + output
# -----------------------
def get_routes(routing, solution, time_dim, data, manager):
    routes = []
    for v in range(len(data["vehicle_capacities"])):
        index = routing.Start(v)
        # Unused vehicle if start immediately leads to end
        if routing.IsEnd(solution.Value(routing.NextVar(index))):
            continue

        plan = []
        while not routing.IsEnd(index):
            arrival = solution.Value(time_dim.CumulVar(index))
            node = manager.IndexToNode(index)
            plan.append((node, arrival))
            index = solution.Value(routing.NextVar(index))
        # Add end
        arrival = solution.Value(time_dim.CumulVar(index))
        node = manager.IndexToNode(index)
        plan.append((node, arrival))

        routes.append((v, plan))
    return routes

def to_geojson(routes, data):
    """
    Build a FeatureCollection of LineStrings and Points for each vehicle route.
    """
    features = []
    stops = data["stops"]

    # points
    for i, s in enumerate(stops):
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]},
            "properties": {"index": i, "name": s.name, "demand": s.demand}
        })

    # lines per vehicle
    for v, plan in routes:
        coords = [(stops[i].lat, stops[i].lon) for (i, _) in plan]
        # fetch segment polylines between consecutive points
        line_coords = []
        for (a_idx, _), (b_idx, _) in zip(plan[:-1], plan[1:]):
            a = (stops[a_idx].lat, stops[a_idx].lon)
            b = (stops[b_idx].lat, stops[b_idx].lon)
            r = osrm_route_between(a, b)
            geom = r["routes"][0]["geometry"]
            seg = polyline.decode(geom)  # [(lat, lon), ...]
            line_coords.extend([(lon, lat) for (lat, lon) in seg])
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line_coords},
            "properties": {"vehicle": data["vehicles"][v].name}
        })

    return {"type": "FeatureCollection", "features": features}

def slugify(value: str) -> str:
    """Convert a string to a filesystem-friendly slug."""
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    cleaned = "_".join(filter(None, cleaned.split("_")))
    return cleaned or "route"

def export_assignments_excel(routes, data, durations_min, distances_m, outfile: str = "driver_assignments.xlsx"):
    stops = data["stops"]
    rows = []
    for vehicle_index, plan in routes:
        vehicle = data["vehicles"][vehicle_index]
        load = 0
        for order, (node, arrival) in enumerate(plan, start=1):
            stop = stops[node]
            prev_node = plan[order - 2][0] if order > 1 else None
            leg_minutes = 0
            leg_distance = 0.0
            if prev_node is not None:
                leg_minutes = durations_min[prev_node][node] or 0
                leg_distance = (distances_m[prev_node][node] or 0.0) / 1000.0
            load += stop.demand
            rows.append({
                "Driver": vehicle.name,
                "Sequence": order,
                "Stop Index": node,
                "Stop Name": stop.name,
                "Latitude": stop.lat,
                "Longitude": stop.lon,
                "Demand": stop.demand,
                "Cumulative Demand": load,
                "ETA (minutes)": arrival,
                "ETA (HH:MM)": f"{arrival // 60:02d}:{arrival % 60:02d}",
                "Leg Minutes": leg_minutes,
                "Leg Distance (km)": round(leg_distance, 3),
            })

    if not rows:
        print("No routes to export to Excel.")
        return

    df = pd.DataFrame(rows)
    df.sort_values(["Driver", "Sequence"], inplace=True)
    with pd.ExcelWriter(outfile, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Assignments")
    print(f"Wrote {outfile}")

def export_routes_kml(routes_geojson, routes, data, folder: str = "routes_kml"):
    path = Path(folder)
    path.mkdir(exist_ok=True)

    # Map vehicle name to stop plan for point annotations
    plan_map = {data["vehicles"][vehicle_index].name: plan for vehicle_index, plan in routes}
    stops = data["stops"]

    for feature in routes_geojson["features"]:
        if feature["geometry"]["type"] != "LineString":
            continue
        vehicle_name = feature["properties"].get("vehicle", "Route")
        coords = feature["geometry"]["coordinates"]
        plan = plan_map.get(vehicle_name, [])

        coords_str = "\n          ".join(f"{lon},{lat},0" for lon, lat in coords)
        placemark_points = []
        for seq, (node, arrival) in enumerate(plan, start=1):
            stop = stops[node]
            placemark_points.append(
                f"""
        <Placemark>
          <name>{seq:02d} - {stop.name}</name>
          <description>ETA {arrival // 60:02d}:{arrival % 60:02d}, Demand {stop.demand}</description>
          <Point>
            <coordinates>{stop.lon},{stop.lat},0</coordinates>
          </Point>
        </Placemark>
        """.strip()
            )

        placemark_block = "\n    ".join(placemark_points)

        kml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{vehicle_name} Route</name>
    <Placemark>
      <name>{vehicle_name} Path</name>
      <Style>
        <LineStyle>
          <color>ff0055ff</color>
          <width>4</width>
        </LineStyle>
      </Style>
      <LineString>
        <tessellate>1</tessellate>
        <coordinates>
          {coords_str}
        </coordinates>
      </LineString>
    </Placemark>
    {placemark_block}
  </Document>
</kml>
"""
        filepath = path / f"{slugify(vehicle_name)}.kml"
        filepath.write_text(kml_content.strip(), encoding="utf-8")
        print(f"Wrote {filepath}")

def quick_map(routes_geojson: Dict[str, Any], outfile: str = "map.html"):
    # center on depot
    dep = next(f for f in routes_geojson["features"] if f["geometry"]["type"] == "Point" and f["properties"]["index"] == 0)
    lat0 = dep["geometry"]["coordinates"][1]
    lon0 = dep["geometry"]["coordinates"][0]
    m = folium.Map(location=[lat0, lon0], zoom_start=12)
    # add stops
    for f in routes_geojson["features"]:
        if f["geometry"]["type"] == "Point":
            folium.CircleMarker(
                location=[f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]],
                radius=5,
                tooltip=f["properties"]["name"]
            ).add_to(m)
    # add lines
    for f in routes_geojson["features"]:
        if f["geometry"]["type"] == "LineString":
            folium.PolyLine(
                locations=[(lat, lon) for lon, lat in f["geometry"]["coordinates"]],
                weight=4,
                tooltip=f["properties"]["vehicle"]
            ).add_to(m)

    m.save(outfile)
    print(f"Wrote {outfile}")

def main():
    print("Building data model and requesting OSRM table...")
    data = build_data_model(LOCATIONS, VEHICLES)

    print("Solving VRP...")
    routing, solution, time_dim, manager = solve_vrp(data)
    if solution is None:
        print("No solution found.")
        return

    print("\n=== Assignment ===")
    routes = get_routes(routing, solution, time_dim, data, manager)
    for v, plan in routes:
        print(f"\n{data['vehicles'][v].name}:")
        for idx, arr in plan:
            s = data["stops"][idx]
            hh = arr // 60
            mm = arr % 60
            print(f"  {idx:>2}  {s.name:<10}  ETA {hh:02d}:{mm:02d}")

    print("\nFetching polylines & writing GeoJSON...")
    gj = to_geojson(routes, data)
    with open("routes.geojson", "w", encoding="utf-8") as f:
        json.dump(gj, f, ensure_ascii=False, indent=2)
    print("Wrote routes.geojson")

    export_assignments_excel(routes, data, data["duration_matrix_min"], data["distance_matrix_m"])
    export_routes_kml(gj, routes, data)
    quick_map(gj, "map.html")

if __name__ == "__main__":
    main()
