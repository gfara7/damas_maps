# Deployment & Operations Guide

This document explains how to take the Damascus Delivery Planner from source code to a running deployment on Azure Container Apps using Docker Hub as the registry.

---

## Overview

```
Local dev -> Docker build -> docker push -> Azure Container Apps (damas-app + osrm)
                                                |
                                                -> Azure Files (OSRM dataset)
```

Two container apps are deployed into the same Container Apps environment:

1. `damas-app` - the Flask API + front-end (port 8000)
2. `osrm` - the routing backend (port 5000)

`osrm` mounts an Azure Files share (`osrmdata`) to load the `.osrm*` graph. At startup it copies the data into `/work` (EmptyDir) for faster reads.

---

## Prerequisites

* [Docker Desktop](https://www.docker.com/products/docker-desktop/)
* Azure CLI 2.58 or newer (2.77 used here)
* Azure subscription with Container Apps enabled
* Docker Hub account (or Azure Container Registry)

Environment variables used below:

```powershell
$SUB   = "<subscription-id>"
$RG    = "rg-damas"
$LOC   = "westeurope"
$ENV   = "cae-damas"
$STOR  = "damasmapsst"
$SHARE = "osrmdata"
$APP_IMG  = "geofarah/damas-app:v3"
$OSRM_IMG = "osrm/osrm-backend:latest" # or geofarah/osrm-syria:latest
```

---

## 1. Build & Push Images

```powershell
docker build -t $APP_IMG .
docker push $APP_IMG

# optional: push your retagged OSRM image
# docker push geofarah/osrm-syria:latest
```

---

## 2. Prepare Azure Resources

```powershell
az group create -n $RG -l $LOC

az monitor log-analytics workspace create `
  -g $RG -n "law-$ENV" -l $LOC
$WSID  = az monitor log-analytics workspace show -g $RG -n "law-$ENV" --query customerId -o tsv
$WSKEY = az monitor log-analytics workspace get-shared-keys -g $RG -n "law-$ENV" --query primarySharedKey -o tsv

az containerapp env create `
  -g $RG -n $ENV -l $LOC `
  --logs-workspace-id $WSID `
  --logs-workspace-key $WSKEY
```

### Azure Files Share

```powershell
az storage account create -g $RG -n $STOR -l $LOC --sku Standard_LRS
az storage share-rm create --resource-group $RG --storage-account $STOR --name $SHARE
# Upload data/*.osrm* to the share
az storage file upload-batch --source ./data --destination $SHARE --account-name $STOR

$STOR_KEY = az storage account keys list -g $RG -n $STOR --query [0].value -o tsv

az containerapp env storage set `
  -g $RG -n $ENV `
  --storage-name osrmfiles `
  --azure-file-account-name $STOR `
  --azure-file-account-key $STOR_KEY `
  --azure-file-share-name $SHARE `
  --access-mode ReadOnly
```

---

## 3. Deploy Container Apps

### OSRM service

The provided `infra/osrm.yaml` uses HTTPS placeholders. When using CLI replace `<MANAGED_ENV_ID>` etc., or use `osrm-update.json` via `az rest`.

Example with JSON (copies data into `/work`):

```powershell
az rest --method put `
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.App/containerApps/osrm?api-version=2023-05-01" `
  --body @osrm-update.json `
  --headers "Content-Type=application/json"
```

### Flask app

```powershell
az containerapp create `
  -g $RG -n damas-app `
  --environment $ENV `
  --image $APP_IMG `
  --ingress external --target-port 8000 `
  --cpu 0.5 --memory 1Gi `
  --min-replicas 0 --max-replicas 1 `
  --env-vars OSRM_BASE=http://osrm:5000
```

---

## 4. Configure Scaling

* Keep `damas-app` at `min-replicas 0` for cost efficiency.
* Optionally keep OSRM warm by setting `min-replicas 1`:
  ```powershell
  az containerapp update -g $RG -n osrm --min-replicas 1 --max-replicas 1
  ```

---

## 5. Verification Checklist

1. **OSRM health**  
   ```powershell
   $OSRM_FQDN = az containerapp show -g $RG -n osrm --query properties.configuration.ingress.fqdn -o tsv
   curl "http://$OSRM_FQDN/health"
   ```
2. **App health**  
   ```powershell
   $APP_FQDN = az containerapp show -g $RG -n damas-app --query properties.configuration.ingress.fqdn -o tsv
   curl "https://$APP_FQDN/api/health"
   ```
3. **Solve test batch** via UI or `curl`:
   ```powershell
   curl -X POST "https://$APP_FQDN/api/solve" `
     -H "Content-Type: application/json" `
     -d @samples/sample-solve.json
   ```

---

## 6. Updating the App

```powershell
docker build -t geofarah/damas-app:v4 .
docker push geofarah/damas-app:v4
az containerapp update -g $RG -n damas-app --image geofarah/damas-app:v4 --set-env-vars OSRM_BASE=http://osrm:5000
```

---

## 7. Cleanup

```powershell
az group delete -n $RG --no-wait --yes
```

---

## Notes & Tips

* When `min-replicas` is 0, the first request may see "OSRM backend is starting up" while the replica warms. The Flask app now retries for ~60 s before giving up.
* Mounting Azure Files and copying to `/work` front-loads transaction cost but yields faster routing responses.
* To avoid leaking subscription/tenant IDs in public repos, parameterize `<MANAGED_ENV_ID>` in YAML files before publishing.

Happy routing!
