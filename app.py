#!/usr/bin/env python3
"""
Lightweight Flask server that exposes the VRP solver through a JSON API
and serves a minimal frontend for collecting stops and visualising routes.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests
from flask import Flask, jsonify, request

from solve_vrp import (
    Stop,
    Vehicle,
    build_data_model,
    get_routes,
    solve_vrp,
    to_geojson,
)

app = Flask(__name__, static_folder="static", static_url_path="")


def _parse_stop(raw: Dict[str, Any], idx: int, *, is_depot: bool) -> Stop:
    name = (raw.get("name") or "").strip() or ("Depot" if is_depot else f"Stop {idx}")
    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Stop #{idx + 1} has invalid latitude/longitude") from exc

    demand_default = 0 if is_depot else 1
    service_default = 0 if is_depot else 5
    try:
        demand = int(raw.get("demand", demand_default))
        service_min = int(raw.get("service_min", service_default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Stop #{idx + 1} has invalid demand or service minutes") from exc

    tw_start = raw.get("tw_start")
    tw_end = raw.get("tw_end")
    time_window: Optional[tuple[int, int]] = None
    if tw_start is not None and tw_end is not None:
        try:
            start = int(tw_start)
            end = int(tw_end)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Stop #{idx + 1} has invalid time window values") from exc
        if end < start:
            raise ValueError(f"Stop #{idx + 1} has time window end before start")
        time_window = (start, end)

    return Stop(
        name=name,
        lat=lat,
        lon=lon,
        demand=max(demand, 0),
        tw=time_window,
        service_min=max(service_min, 0),
    )


def _parse_vehicle(raw: Dict[str, Any], idx: int, *, default_capacity: int) -> Vehicle:
    name = (raw.get("name") or "").strip() or f"Vehicle {idx + 1}"
    try:
        capacity = int(raw.get("capacity", default_capacity))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Vehicle #{idx + 1} has invalid capacity") from exc
    capacity = max(capacity, 1)

    try:
        start_index = int(raw.get("start_index", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Vehicle #{idx + 1} has invalid start index") from exc

    end_field = raw.get("end_index")
    end_index: Optional[int]
    if end_field is None or end_field == "":
        end_index = None
    else:
        try:
            end_index = int(end_field)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Vehicle #{idx + 1} has invalid end index") from exc

    max_route_min_field = raw.get("max_route_min")
    if max_route_min_field in (None, "", "null"):
        max_route_min: Optional[int] = None
    else:
        try:
            max_route_min = int(max_route_min_field)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Vehicle #{idx + 1} has invalid max route minutes") from exc

    speed_factor_field = raw.get("speed_factor")
    if speed_factor_field in (None, "", "null"):
        speed_factor = 1.0
    else:
        try:
            speed_factor = float(speed_factor_field)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Vehicle #{idx + 1} has invalid speed factor") from exc

    return Vehicle(
        name=name,
        capacity=capacity,
        start_index=start_index,
        end_index=end_index,
        max_route_min=max_route_min,
        speed_factor=speed_factor,
    )


def _format_routes(routes, data) -> List[Dict[str, Any]]:
    duration_matrix = data["duration_matrix_min"]
    distance_matrix = data["distance_matrix_m"]
    formatted = []
    for vehicle_index, plan in routes:
        vehicle = data["vehicles"][vehicle_index]
        stops_out = []
        total_drive_min = 0
        total_distance_m = 0.0
        prev_idx: Optional[int] = None

        for node_idx, arrival in plan:
            stop = data["stops"][node_idx]
            leg = None
            if prev_idx is not None:
                leg_minutes = duration_matrix[prev_idx][node_idx]
                leg_distance = distance_matrix[prev_idx][node_idx]
                leg = {
                    "from_index": prev_idx,
                    "to_index": node_idx,
                    "drive_minutes": leg_minutes,
                    "distance_m": leg_distance,
                }
                if leg_minutes:
                    total_drive_min += leg_minutes
                if leg_distance:
                    total_distance_m += leg_distance

            stops_out.append(
                {
                    "index": node_idx,
                    "name": stop.name,
                    "lat": stop.lat,
                    "lon": stop.lon,
                    "arrival_min": arrival,
                    "arrival_hhmm": f"{arrival // 60:02d}:{arrival % 60:02d}",
                    "demand": stop.demand,
                    "service_min": stop.service_min,
                    "time_window": list(stop.tw) if stop.tw else None,
                    "leg": leg,
                }
            )
            prev_idx = node_idx

        formatted.append(
            {
                "vehicle": vehicle.name,
                "vehicle_index": vehicle_index,
                "stops": stops_out,
                "total_drive_min": total_drive_min,
                "total_distance_m": total_distance_m,
            }
        )
    return formatted


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.post("/api/solve")
def api_solve():
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected JSON body with stops array"}), 400

    raw_stops = payload.get("stops")
    if not isinstance(raw_stops, list) or not raw_stops:
        return jsonify({"error": "At least one stop (including depot) is required"}), 400

    try:
        stops = [
            _parse_stop(raw, idx, is_depot=(idx == 0))
            for idx, raw in enumerate(raw_stops)
        ]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if len(stops) < 2:
        return jsonify({"error": "Provide at least a depot and one delivery stop"}), 400

    total_demand = sum(stop.demand for stop in stops)
    raw_vehicles = payload.get("vehicles")

    if raw_vehicles:
        if not isinstance(raw_vehicles, list):
            return jsonify({"error": "vehicles must be a list"}), 400
        try:
            vehicles = [
                _parse_vehicle(raw, idx, default_capacity=max(total_demand, 1))
                for idx, raw in enumerate(raw_vehicles)
            ]
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        vehicles = [
            Vehicle(
                name="Courier 1",
                capacity=max(total_demand, 1),
                start_index=0,
                end_index=None,
            )
        ]

    if any(v.start_index >= len(stops) or v.start_index < 0 for v in vehicles):
        return jsonify({"error": "Vehicle start_index out of range"}), 400
    if any(v.end_index is not None and (v.end_index >= len(stops) or v.end_index < 0) for v in vehicles):
        return jsonify({"error": "Vehicle end_index out of range"}), 400

    try:
        build_start = time.perf_counter()
        data = build_data_model(stops, vehicles)
        build_ms = (time.perf_counter() - build_start) * 1000.0
    except requests.RequestException as exc:
        return (
            jsonify(
                {
                    "error": "Failed to reach OSRM backend",
                    "details": str(exc),
                }
            ),
            503,
        )

    solve_start = time.perf_counter()
    routing, solution, time_dim = solve_vrp(data)
    solve_ms = (time.perf_counter() - solve_start) * 1000.0

    if solution is None:
        return jsonify({"error": "No feasible solution found"}), 422

    routes = get_routes(routing, solution, time_dim, data)
    formatted_routes = _format_routes(routes, data)

    geojson: Optional[Dict[str, Any]]
    try:
        geojson = to_geojson(routes, data)
    except requests.RequestException:
        # If polylines fail we still return a result; the frontend will degrade gracefully.
        geojson = None

    return jsonify(
        {
            "routes": formatted_routes,
            "geojson": geojson,
            "meta": {
                "stops": len(stops),
                "vehicles": len(vehicles),
                "build_ms": round(build_ms, 1),
                "solve_ms": round(solve_ms, 1),
            },
        }
    )


@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)
