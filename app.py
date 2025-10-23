#!/usr/bin/env python3
"""
Lightweight Flask server that exposes the VRP solver through a JSON API
and serves a minimal frontend for collecting stops and visualising routes.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import io
import zipfile
import json
import requests
import pandas as pd
from flask import Flask, jsonify, request, send_file

from solve_vrp import (
    Stop,
    Vehicle,
    build_data_model,
    get_routes,
    solve_vrp,
    to_geojson,
    slugify,
)

app = Flask(__name__, static_folder="static", static_url_path="")


# Simple in-memory cache of the latest successful solve so export endpoints
# can reuse matrices and routes without re-solving.
_LAST_SOLVE: Optional[Dict[str, Any]] = None


def _canonical_key(stops: List[Stop], vehicles: List[Vehicle]) -> str:
    """Build a stable string key representing the inputs (order-sensitive)."""
    def ser_stop(s: Stop) -> Dict[str, Any]:
        return {
            "name": s.name,
            # round for stability against minor float formatting differences
            "lat": round(float(s.lat), 6),
            "lon": round(float(s.lon), 6),
            "demand": int(s.demand),
            "service_min": int(s.service_min),
            "tw": list(s.tw) if s.tw else None,
        }

    def ser_vehicle(v: Vehicle) -> Dict[str, Any]:
        return {
            "name": v.name,
            "capacity": int(v.capacity),
            "start_index": int(v.start_index),
            "end_index": (int(v.end_index) if v.end_index is not None else None),
            "max_route_min": (int(v.max_route_min) if v.max_route_min is not None else None),
            # keep a reasonable precision for stability
            "speed_factor": float(f"{float(v.speed_factor):.3f}"),
        }

    canon = {
        "stops": [ser_stop(s) for s in stops],
        "vehicles": [ser_vehicle(v) for v in vehicles],
    }
    return json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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

@app.get("/favicon.ico")
def favicon():
    return ("", 204)



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
    routing, solution, time_dim, manager = solve_vrp(data)
    solve_ms = (time.perf_counter() - solve_start) * 1000.0

    if solution is None:
        return jsonify({"error": "No feasible solution found"}), 422

    routes = get_routes(routing, solution, time_dim, data, manager)
    formatted_routes = _format_routes(routes, data)

    geojson: Optional[Dict[str, Any]]
    try:
        geojson = to_geojson(routes, data)
    except requests.RequestException:
        # If polylines fail we still return a result; the frontend will degrade gracefully.
        geojson = None

    meta = {
        "stops": len(stops),
        "vehicles": len(vehicles),
        "build_ms": round(build_ms, 1),
        "solve_ms": round(solve_ms, 1),
    }

    # Cache the latest successful solve for reuse in export endpoints
    global _LAST_SOLVE
    try:
        _LAST_SOLVE = {
            "key": _canonical_key(stops, vehicles),
            "routes": routes,
            "formatted_routes": formatted_routes,
            "data": data,
            "geojson": geojson,
            "meta": meta,
        }
    except Exception:
        # Never fail the response due to caching issues
        pass

    return jsonify({"routes": formatted_routes, "geojson": geojson, "meta": meta})


@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok"})


@app.get("/map/latest")
def map_latest():
    """Render the latest solved plan as an HTML Leaflet map.
    Requires that /api/solve has been called successfully at least once.
    """
    global _LAST_SOLVE
    if not _LAST_SOLVE or not _LAST_SOLVE.get("geojson"):
        return (
            "<html><body style='font-family:sans-serif;padding:1rem'>"
            "No solved routes available. Solve a plan first."
            "</body></html>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    gj = _LAST_SOLVE["geojson"]
    # Inline minimal page with Leaflet and the cached GeoJSON
    gj_json = json.dumps(gj, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Latest Route Map</title>
    <link rel=\"stylesheet\" href=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.css\" />
    <style>html,body,#map{{height:100%;margin:0}}</style>
  </head>
  <body>
    <div id=\"map\"></div>
    <script src=\"https://unpkg.com/leaflet@1.9.4/dist/leaflet.js\"></script>
    <script>
      const geojson = {gj_json};
      const map = L.map('map');
      L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap contributors'
      }}).addTo(map);

      // Deterministic color per vehicle name
      const palette = ['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf'];
      const colorFor = (name) => {{
        let h = 0; const s = String(name || '');
        for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
        return palette[Math.abs(h) % palette.length];
      }};

      const layer = L.geoJSON(geojson, {{
        pointToLayer: function(feature, latlng) {{
          const isDepot = feature.properties && feature.properties.index === 0;
          const color = isDepot ? '#2563eb' : '#f97316';
          return L.circleMarker(latlng, {{ radius: isDepot ? 7 : 5, color, fillColor: color, fillOpacity: 0.85, weight: 2 }}).bindTooltip((feature.properties && feature.properties.name) || '');
        }},
        style: function(feature) {{
          if (feature && feature.geometry && feature.geometry.type === 'LineString') {{
            const v = feature.properties && feature.properties.vehicle;
            return {{ color: colorFor(v), weight: 4, opacity: 0.85 }};
          }}
          return {{}};
        }},
        onEachFeature: function(feature, layer) {{
          if (feature && feature.geometry && feature.geometry.type === 'LineString') {{
            const v = (feature.properties && feature.properties.vehicle) || 'Route';
            layer.bindPopup('<strong>Vehicle:</strong> ' + v);
            layer.on({{
              mouseover: () => layer.setStyle({{ weight: 6, opacity: 1 }}),
              mouseout: () => layer.setStyle({{ weight: 4, opacity: 0.85 }})
            }});
          }}
        }}
      }}).addTo(map);
      if (layer.getBounds && layer.getBounds().isValid()) {{
        map.fitBounds(layer.getBounds(), {{ padding: [20, 20] }});
      }} else {{
        map.setView([0, 0], 2);
      }}
    </script>
  </body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/export/assignments", methods=["POST"])
def api_export_assignments():
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

    total_demand = sum(stop.demand for stop in stops)
    raw_vehicles = payload.get("vehicles")
    if raw_vehicles:
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
            )
        ]

    # Try to reuse last solve if inputs match; otherwise compute fresh
    use_cached = False
    global _LAST_SOLVE
    key = _canonical_key(stops, vehicles)
    if _LAST_SOLVE and _LAST_SOLVE.get("key") == key:
        data = _LAST_SOLVE["data"]
        routes = _LAST_SOLVE["routes"]
        use_cached = True
    else:
        try:
            data = build_data_model(stops, vehicles)
            routing, solution, time_dim, manager = solve_vrp(data)
        except requests.RequestException as exc:
            return jsonify({"error": "Failed to reach OSRM backend", "details": str(exc)}), 503

        if solution is None:
            return jsonify({"error": "No feasible solution found"}), 422

        routes = get_routes(routing, solution, time_dim, data, manager)
        # Update cache since we computed a fresh solution
        try:
            _LAST_SOLVE = {
                "key": key,
                "routes": routes,
                "formatted_routes": _format_routes(routes, data),
                "data": data,
                "geojson": None,
                "meta": None,
            }
        except Exception:
            pass

    duration_matrix = data["duration_matrix_min"]
    distance_matrix = data["distance_matrix_m"]
    rows = []
    for vehicle_index, plan in routes:
        vehicle = data["vehicles"][vehicle_index]
        load = 0
        for order, (node, arrival) in enumerate(plan, start=1):
            stop = data["stops"][node]
            prev_node = plan[order - 2][0] if order > 1 else None
            leg_minutes = duration_matrix[prev_node][node] if prev_node is not None else 0
            leg_distance = (distance_matrix[prev_node][node] if prev_node is not None else 0.0) / 1000.0
            load += stop.demand
            rows.append(
                {
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
                    "Leg Minutes": leg_minutes or 0,
                    "Leg Distance (km)": round(leg_distance or 0.0, 3),
                }
            )

    if not rows:
        return jsonify({"error": "No routes to export"}), 400

    df = pd.DataFrame(rows)
    df.sort_values(["Driver", "Sequence"], inplace=True)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Assignments")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="driver_assignments.xlsx",
    )


@app.route("/api/export/kmlzip", methods=["POST"])
def api_export_kmlzip():
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

    total_demand = sum(stop.demand for stop in stops)
    raw_vehicles = payload.get("vehicles")
    if raw_vehicles:
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
            )
        ]

    # Try to reuse last solve + geojson
    global _LAST_SOLVE
    key = _canonical_key(stops, vehicles)
    gj = None
    if _LAST_SOLVE and _LAST_SOLVE.get("key") == key:
        data = _LAST_SOLVE["data"]
        routes = _LAST_SOLVE["routes"]
        gj = _LAST_SOLVE.get("geojson")
    else:
        try:
            data = build_data_model(stops, vehicles)
            routing, solution, time_dim, manager = solve_vrp(data)
        except requests.RequestException as exc:
            return jsonify({"error": "Failed to reach OSRM backend", "details": str(exc)}), 503

        if solution is None:
            return jsonify({"error": "No feasible solution found"}), 422

        routes = get_routes(routing, solution, time_dim, data, manager)

    if gj is None:
        try:
            gj = to_geojson(routes, data)
            # store geojson in cache for future reuse
            if _LAST_SOLVE and _LAST_SOLVE.get("key") == key:
                _LAST_SOLVE["geojson"] = gj
        except requests.RequestException as exc:
            return jsonify({"error": "Failed to fetch route polylines", "details": str(exc)}), 502

    stops_list = data["stops"]
    plan_map = {data["vehicles"][vi].name: plan for vi, plan in routes}
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for feature in gj.get("features", []):
            if feature.get("geometry", {}).get("type") != "LineString":
                continue
            vehicle_name = feature.get("properties", {}).get("vehicle", "Route")
            coords = feature.get("geometry", {}).get("coordinates", [])
            plan = plan_map.get(vehicle_name, [])

            coords_str = "\n          ".join(f"{lon},{lat},0" for lon, lat in coords)
            placemark_points = []
            for seq, (node, arrival) in enumerate(plan, start=1):
                stop = stops_list[node]
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

            kml_content = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<kml xmlns=\"http://www.opengis.net/kml/2.2\">
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
    {'\\n    '.join(placemark_points)}
  </Document>
</kml>
"""
            fname = f"{slugify(vehicle_name)}.kml"
            zf.writestr(fname, kml_content.strip())

    mem.seek(0)
    return send_file(
        mem,
        mimetype="application/zip",
        as_attachment=True,
        download_name="routes_kml.zip",
    )




if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8000)


