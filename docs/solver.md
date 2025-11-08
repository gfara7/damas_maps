# Solver & Algorithm Notes

This document explains how `solve_vrp.py` builds the routing problem, interacts with OSRM, and produces the final outputs.

---

## Data Flow

```
User payload (stops + vehicles)
    ->
Flask API parses + validates
    ->
solve_vrp.build_data_model()
    -> OSRM /table (distance & duration matrices)
    -> Apply service times, capacities, time windows
    -> Create OR-Tools routing model
    ->
OR-Tools solve_vrp()
    -> PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH (20 s)
    -> Extract routes, arrival times
    ->
Post-processing
    -> Format JSON response
    -> Request OSRM /route per leg for polylines (GeoJSON)
    -> Export helpers (Excel, KML, HTML map)
```

---

## OSRM Integration

### `/table`

* Called once per solve (`build_data_model`)
* Coordinates list is `[(lat, lon), ...]` with depot at index 0
* Query string example:
  ```
  http://osrm:5000/table/v1/driving/lat1,lon1;lat2,lon2;...
      ?sources=0;1;...
      &destinations=0;1;...
      &annotations=duration,distance
  ```
* Returns duration in seconds, distance in metres
* Durations converted to whole minutes (`int(round(sec/60))`)

### `/route`

* Once per consecutive pair `i -> j` for each vehicle
* Used only for GeoJSON & KML exports
* Example:
  ```
  http://osrm:5000/route/v1/driving/lonA,latA;lonB,latB?overview=full&geometries=polyline
  ```

### Resilience

* When OSRM is cold (scaled to zero), the Flask app calls `/health` and retries up to ~60 seconds before raising a user-friendly error.
* If `/route` requests fail, the API returns the solution without polylines (front-end degrades gracefully).

---

## OR-Tools Model

* **RoutingIndexManager**: indexes 0..N-1 nodes, vehicles K, with start/end per vehicle
* **RoutingModel**: built around the following dimensions:

1. **Cost / Transit Callback**
   ```python
   duration_matrix[i][j] + service_min[i]
   ```
   * Each arc cost = travel minutes + service time at origin
   * Service time ensures we pay for dwell time when leaving a stop

2. **Capacity Dimension**
   * Demand = stop.demand (non-negative)
   * Vehicle capacity default = max(total demand, 1) if not provided
   * Hard constraint: cumulative load <= capacity

3. **Time Dimension**
   * Horizon: 24h (1440 minutes)
   * Time windows per stop (`tw`), inclusive
   * Vehicle start/end windows set to depot window (or default [0, 1440])
   * Optional max route duration by constraining end <= start + limit

4. **Search Parameters**
   ```python
   FirstSolutionStrategy = PATH_CHEAPEST_ARC
   LocalSearchMetaheuristic = GUIDED_LOCAL_SEARCH
   TimeLimit = 20 seconds
   ```
   * 20 s is ample for tens of stops; adjust upward for larger instances

5. **Solution Extraction**
   * Iterate each vehicle's route, capturing `(node, arrival_time)`
   * Compute per-leg travel time/distance from the matrices
   * Keep totals for reporting (drive time, distance)

---

## Exports

* **GeoJSON**: Points for each stop + LineString per vehicle with decoded polyline
* **Excel** (`export_assignments_excel`):
  * Columns: Driver, Sequence, Stop Index, Name, Demand, cumulative load, ETA, leg stats
* **KML** (`export_routes_kml` / API KML ZIP):
  * Per-vehicle file with placemarks for each stop, route polyline, and metadata
* **HTML Map** (`quick_map`):
  * Folium mini-map generated for CLI usage (`solve_vrp.py main`)

All exports sort routes by driver and sequence to ensure deterministic presentation.

---

## Tips for Tuning

1. **Large Instances** (100+ stops)
   * Increase OSRM chunk size or switch `/table` to POST
   * Increase OR-Tools time limit (`search_params.time_limit.seconds`)
   * Consider parallel OSRM replicas if concurrency is high

2. **Strict Time Windows**
   * Ensure depot windows accommodate all vehicles
   * Add slack to time dimension if early arrival needs waiting

3. **Realistic Durations**
   * Use custom OSRM profile (see `profiles/car.lua`) to adjust speeds
   * Optionally add per-vehicle `speed_factor` fudge (handled in solver)

4. **Warm Starts / Caching**
   * `_LAST_SOLVE` cache avoids recompute when exports use same payload
   * GeoJSON cache prevents duplicate `/route` calls unless payload changes

---

## CLI Utilities

* `plan_daily.py` - Batch solver that reads JSON, writes routes, Excel, KML, map
* `fetch_shops.py` - Retrieves store POIs from Overpass (amenity/shop filters)

Both reuse the same solver stack described above.

---

## References

* [OSRM Wiki](https://github.com/Project-OSRM/osrm-backend/wiki)
* [OR-Tools Vehicle Routing](https://developers.google.com/optimization/routing)
* [Azure Container Apps Docs](https://learn.microsoft.com/azure/container-apps/)
* `solve_vrp.py` in this repo for concrete implementation details
