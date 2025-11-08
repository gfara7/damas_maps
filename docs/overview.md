# Project Overview & Delivery Workflow

## What the Repository Provides

1. **Interactive Planning UI**  
   * Leaflet-based map for depot/stops entry  
   * Store catalog search (datalist)  
   * Vehicle overrides, batch save/load, exports (Excel & KML)

2. **Flask API**  
   * `/api/solve` - OR-Tools VRP with OSRM travel times  
   * `/api/export/assignments`, `/api/export/kmlzip` - reuse cached solution  
   * `/api/shops` - serves the store catalog from CSV/GeoJSON

3. **Routing Backend (OSRM)**  
   * MLD graph prepared via `MakeFile` + Docker pipeline  
   * Custom car profile (`profiles/car.lua`) to slow certain roads  
   * Optional CLI tool (`plan_daily.py`) that produces the same outputs batch-style

4. **Tooling**  
   * `fetch_shops.py` to collect supermarket/cafe/restaurant POIs  
   * `solve_vrp.py` contains reusable solver logic + export helpers  
   * `infra/*.yaml` + JSON for Azure Container Apps

---

## Docker -> Hub -> Container Apps Flow

### 1. Local Build

```bash
docker build -t geofarah/damas-app:v3 .
docker build -t geofarah/osrm-syria:latest Dockerfile.osrm   # optional
```

### 2. Push to Registry

```bash
docker push geofarah/damas-app:v3
docker push geofarah/osrm-syria:latest
```

Docker Hub holds the images in public repos for easy pulls by Azure Container Apps.

### 3. Azure Resources

* Resource Group (`rg-damas`)
* Container Apps Environment (`cae-damas`)
* Azure Files share (`osrmdata`) populated with `.osrm*`
* Log Analytics workspace for diagnostics

### 4. Container App Deployment

* `osrm` container:
  * Mounts `osrmfiles` share at `/.data`
  * Copies dataset to `/work` (EmptyDir)
  * Runs `osrm-routed --algorithm mld /work/syria-latest.osrm`
* `damas-app` container:
  * Exposes port 8000 (HTTP ingress)
  * Env var `OSRM_BASE=http://osrm:5000`
  * Scales to zero when idle (optional)

Deployment options:

```bash
az containerapp create -g rg-damas -n osrm --environment cae-damas --yaml infra/osrm.yaml
az containerapp create -g rg-damas -n damas-app --environment cae-damas --yaml infra/damas-app.yaml
```

or use the provided JSON + `az rest` to update the OSRM template.

### 5. Verification

```bash
$OSRM_FQDN = az containerapp show -g rg-damas -n osrm --query properties.configuration.ingress.fqdn -o tsv
curl "http://$OSRM_FQDN/health"

$APP_FQDN = az containerapp show -g rg-damas -n damas-app --query properties.configuration.ingress.fqdn -o tsv
curl "https://$APP_FQDN/api/health"
```

Once both return `{"status":"ok"}`, the UI is accessible and solves will succeed.

---

## Cold-Start Strategy

* By default `damas-app` and `osrm` are set to `min-replicas 0` to save cost.  
* On the first request the API calls `ensure_osrm_ready()`, which pings `/health` up to ~60 seconds before showing a retry message.
* If you need instant responses, set `min-replicas` to 1 on both apps:
  ```bash
  az containerapp update -g rg-damas -n osrm --min-replicas 1 --max-replicas 1
  az containerapp update -g rg-damas -n damas-app --min-replicas 1 --max-replicas 1
  ```

---

## Key Files

| File / Folder       | Purpose                                           |
|---------------------|----------------------------------------------------|
| `app.py`            | Flask API + caching + OSRM warm-up logic           |
| `solve_vrp.py`      | OR-Tools solver, OSRM helpers, export routines     |
| `static/index.html` | Leaflet UI with store catalog + results            |
| `docs/deployment.md`| Detailed deployment cookbook                       |
| `docs/solver.md`    | Algorithm deep dive                                |
| `docs/architecture.drawio` | diagrams.net architecture diagram           |
| `infra/*.yaml`      | Container Apps templates (fill in env IDs)         |
| `MakeFile`          | OSRM data preparation tasks                        |

This cheat-sheet should help new contributors quickly understand the moving parts and reproduce the Docker -> Hub -> Container Apps pipeline.
