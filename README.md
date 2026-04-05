# Carbon-Aware EV Charging System

FastAPI backend and dashboard for tracking India's live grid mix from NPP, estimating carbon intensity, forecasting cleaner charging windows, and recommending an EV charging schedule.

## Run locally

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m uvicorn backend.main:app --reload --reload-dir backend --reload-dir frontend
```

Open `http://127.0.0.1:8000`.

## Main endpoints

- `/grid-data`
- `/carbon-intensity`
- `/forecast`
- `/optimal-charging`
- `/system-status`
- `/debug/grid-status`

## Run tests

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest
```

## Docker

```powershell
docker build -t carbon-aware-ev .
docker run --rm -p 8000:8000 carbon-aware-ev
```

## Deploy with a custom domain

The easiest production path for this project is a Docker-based web service on Render.

### Why this works well

- The project already includes a `Dockerfile`
- Render can build directly from a Dockerfile
- Render supports custom domains and automatic TLS
- The container now respects the platform-provided `PORT`

### Recommended setup

1. Push this project to a GitHub repository
2. In Render, create a new Web Service
3. Connect the GitHub repo and choose the Docker runtime
4. Set the health check path to `/health`
5. Optionally set `APP_DATA_DIR=/var/data` if you attach persistent storage later
6. Deploy the service
7. In the service settings, add your custom domain
8. Add the DNS records Render shows at your domain registrar
9. Verify the domain in Render and wait for TLS issuance

### Notes

- Without persistent storage, cached grid data and forecast history rebuild after a fresh deploy or restart
- If you want longer-lived history, use a persistent disk or external database
- If you add a custom domain, Render also keeps the default `onrender.com` subdomain unless you disable it
