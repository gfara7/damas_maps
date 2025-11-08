# Damascus Delivery Planner

An end-to-end proof-of-concept for planning last-mile deliveries around Damascus.  
The project combines a lightweight Flask API, an interactive Leaflet front-end, an OSRM routing service, and Google OR-Tools to compute vehicle routes, ETAs, Excel manifests, and KML exports.

---

## Contents

1. [Architecture](#architecture)
2. [Features](#features)
3. [Local Development](#local-development)
4. [Azure Deployment Workflow](#azure-deployment-workflow)
5. [Frontend Guide](#frontend-guide)
6. [API Overview](#api-overview)
7. [Solver Details](#solver-details)
8. [Repository Structure](#repository-structure)
9. [Further Reading](#further-reading)

---

## Architecture

The solution is intentionally simple:

* **Frontend & API (`damas-app`)**
  * Flask + Gunicorn
  * Serves the Leaflet UI, handles `/api/solve`, `/api/export/*`
  * Calls OSRM for distance/time matrices, runs OR-Tools locally
* **Routing Backend (`osrm`)**
  * OSRM MLD server with Damascus data (downloaded via `MakeFile`)
  * Loads static `.osrm*` graph files from an Azure Files share and copies them to local disk at startup for performance
* **Storage**
  * Azure Files share (`osrmfiles`) hosts preprocessed `.osrm` assets
  * Optional store catalog CSV/GeoJSON ships with the repo
* **Container Registry**
  * Docker Hub (`geofarah/damas-app` and `geofarah/osrm-syria`) stores build artefacts

A full draw.io diagram is under `docs/architecture.drawio`.

---

## Features

* Rapid entry of depot + stops, or load from a Damascus store catalog (datalist lookup)
* Adjustable vehicle count, capacities, and overrides
* Solve via OR-Tools:
  * Generates per-vehicle sequences, cumulative load, ETAs, per-leg stats
  * Returns GeoJSON for map overlays
* Exports:
  * Excel driver assignments (`.xlsx`)
  * KML files (zipped) with per-stop placemarks
  * Saved "batch" JSON for re-use
* Leaflet UI:
  * draggable map, route cards, store catalog search, load/save batches
  * Supports English and Arabic stop names (inputs are `dir="auto"`)

---

## Local Development

1. **Prerequisites**
   * Python 3.11+
   * Docker Desktop (optional, for local OSRM)
   * `make` + `curl` for data prep

2. **Install dependencies**
   ```bash
   pip install -r requirments.txt
   ```

3. **Prepare OSRM data (Damascus)**
   ```bash
   make -f MakeFile refresh    # download + extract + partition + customize
   ```

4. **Run OSRM locally (Docker)**
   ```bash
   docker run --rm -t -p 5000:5000 \
     -v "$PWD/data:/data" \
     osrm/osrm-backend:latest \
     osrm-routed --algorithm mld /data/syria-latest.osrm
   ```

5. **Run the Flask app**
   ```bash
   export OSRM_BASE=http://localhost:5000   # PowerShell: $env:OSRM_BASE = ...
   python app.py
   ```
   Open `http://localhost:8000`.

---

## Azure Deployment Workflow

A detailed step-by-step write-up is provided in `docs/deployment.md`. High-level summary:

1. Build & push the images:
   ```bash
   docker build -t geofarah/damas-app:latest .
   docker push geofarah/damas-app:latest

   docker push geofarah/osrm-syria:latest   # retagged osrm backend
   ```
2. Provision Azure resources
   * Resource group, Container Apps environment
   * Azure Files share (`osrmdata`) populated with `.osrm*`
3. Deploy container apps (YAML templates under `infra/`)
   ```bash
   az containerapp create -g ... --yaml infra/osrm.yaml
   az containerapp create -g ... --yaml infra/damas-app.yaml
   ```
4. Update environment variables (`OSRM_BASE=http://osrm:5000`)
5. Optional: warm OSRM replica (min replicas = 1) for instant responses

---

## Frontend Guide

* **Store Catalog**: type to filter via datalist; add selected store to stops table.
* **Vehicle Overrides**: set count/capacity before solving; respects loaded batch vehicles.
* **Solve**: first call pings OSRM; if the backend is still waking up you'll see a friendly retry message.
* **Exports**: Excel + KML (zipped) generated from the latest solution; disabled until a solve completes.
* **Save/Load Batch**: persists depot/stops/vehicles to JSON.

---

## API Overview

| Endpoint                   | Method | Description                                  |
|---------------------------|--------|----------------------------------------------|
| `/api/health`             | GET    | Returns `{"status":"ok"}` (liveness probe)   |
| `/api/solve`              | POST   | Body with `stops` + optional `vehicles`; returns routes, geojson, meta |
| `/api/export/assignments` | POST   | Same body, responds with Excel driver manifest |
| `/api/export/kmlzip`      | POST   | Same body, responds with ZIP of KML files     |
| `/api/shops`              | GET    | Loads store catalog (CSV or GeoJSON)          |

Requests require at least a depot (`stops[0]`). Vehicle specs are optional (defaults to a single courier covering all demand).

---

## Solver Details

A thorough explanation lives in `docs/solver.md`. In brief:

1. Fetch OSRM `/table` for duration/distance matrices.
2. Convert durations to whole minutes, add service times, time windows, capacity constraints.
3. Create OR-Tools `RoutingIndexManager` and `RoutingModel`:
   * cost = travel time + service time
   * capacity & time-window dimensions
   * per-vehicle route duration limit support
4. Solve with PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH (20s limit).
5. Extract per-vehicle routes, compute metadata, request per-leg polylines (`/route`).

---

## Repository Structure

```
.github/ci/... (optional pipelines)
docs/
  architecture.drawio     # diagrams.net architecture
  deployment.md           # detailed deployment guide
  solver.md               # algorithm and solver notes
frontend static/
  index.html, assets...
infra/
  damas-app.yaml          # Container App template
  osrm.yaml               # Container App template
MakeFile                  # OSRM data preparation
app.py                    # Flask API + cache logic
solve_vrp.py              # Solver, OSRM helper, exports
plan_daily.py             # CLI batch planner
fetch_shops.py            # Store catalog fetcher
```

---

## Further Reading

* [docs/deployment.md](docs/deployment.md): full Docker -> Docker Hub -> Azure Container Apps workflow
* [docs/solver.md](docs/solver.md): deep dive into OSRM table/route calls and OR-Tools configuration
* [docs/architecture.drawio](docs/architecture.drawio): editable architecture diagram (open in diagrams.net)
* [infra/*.yaml](infra/): Container Apps deployment templates

For production hardening ideas (auth, scaling, monitoring, resiliency) check the "Future Enhancements" section in `docs/deployment.md`.
