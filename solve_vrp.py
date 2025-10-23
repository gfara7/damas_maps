#!/usr/bin/env python3
import math
import time
import json
import requests
import polyline
import folium
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

OSRM_BASE = "http://localhost:5000"  # from docker compose

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
LOCATIONS: List[Stop] = [
    Stop("Depot", 33.513, 36.292, demand=0, tw=(8*60, 20*60), service_min=0),  # Damascus center-ish
    Stop("Shop A", 33.515, 36.300, demand=1, tw=(9*60, 17*60)),
    Stop("Shop B", 33.502, 36.285, demand=1, tw=(10*60, 16*60)),
    Stop("Shop C", 33.520, 36.260, demand=2, tw=(9*60, 18*60)),
    Stop("Shop D", 33.485, 36.315, demand=1),
    Stop("Shop E", 33.540, 36.280, demand=1),
    # add as many as you need...
]

VEHICLES: List[Vehicle] = [
    Vehicle("Truck 1", capacity=3, start_index=0),
    Vehicle("Truck 2", capacity=3, start_index=0),
    # add more vehicles if needed
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

    quick_map(gj, "map.html")

if __name__ == "__main__":
    main()
