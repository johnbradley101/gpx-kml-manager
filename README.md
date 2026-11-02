# GPX & KML Manager

A small self-hosted web app (one Docker container) to **import, store, edit, convert and export GPX and KML files**, built to run on a Synology NAS with Container Manager.

## Features

- **Import** GPX, KML and KMZ (drag & drop, file picker, or drop files into the `inbox` folder on the NAS).
- **Library**: every item is stored as a normal `.gpx` file in a folder you can see in File Station. The original upload is kept untouched in `originals/`.
- **Map view** (OpenStreetMap / OpenTopoMap) with an elevation profile.
- **Track editing** — choose a section with the A and B handles (sliders, drag the A/B pins, click the map, or click the elevation chart), then:
  - **Crop** to A–B
  - **Remove A–B** and split the rest into two tracks, or join the two ends
  - **Split** a track at A
  - also: reverse, rename, merge with the next track, delete a track, add/edit/delete waypoints, rename the file
  - **Undo** (last 20 edits per file are kept)
- **Convert / export** any item as GPX or KML, export the whole library as a zip, or use *Quick convert* to convert a file without storing it.
- Optional password (HTTP basic auth).

## Install on a Synology NAS (Container Manager)

### Option A — build on the NAS (works for any CPU, recommended)

1. In **File Station**, copy this whole folder to the NAS, e.g. `/docker/gpx-kml-manager/` (it must contain `Dockerfile` and `docker-compose.yml`).
2. Open **Container Manager → Project → Create**.
   - Project name: `gpx-kml-manager`
   - Path: the folder from step 1
   - Source: *Use existing docker-compose.yml*
3. (Optional) edit the settings in `docker-compose.yml` first — port, time zone, `APP_USER` / `APP_PASSWORD`.
4. Click through to **Build / Run**. The first build downloads the base images and needs internet access.
5. Open `http://<NAS-IP>:8080`.

Your data lives in `/docker/gpx-kml-manager/data/` (created automatically).

### Option B — build elsewhere, import the image

On a PC with Docker (this builds one image that runs on both Intel/AMD and ARM Synology models):

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t <your-registry>/gpx-kml-manager:latest --push .
```

Then pull it in Container Manager (**Registry**), or build a single architecture and import the file:

```bash
docker buildx build --platform linux/amd64 -t gpx-kml-manager:latest --load .   # or linux/arm64
docker save gpx-kml-manager:latest -o gpx-kml-manager.tar
```

Container Manager → **Image → Add → Import from file**, then create a container with port `8080` and a volume mapping a NAS folder to `/data`.

### Option C — pull the prebuilt image from GitHub

Every push to `main` is tested and published as a multi-architecture image (Intel/AMD and ARM) to GitHub Container Registry as `ghcr.io/<owner>/<repo>:latest`. In `docker-compose.yml`, replace `build: .` with `image: ghcr.io/<owner>/<repo>:latest`. If the repository is private, the package is private too: either make the package public in the repo's *Packages* settings, or log in from the NAS with a personal access token that has `read:packages`.

## Folders (inside `/data`)

| Folder | Purpose |
|---|---|
| `library/` | The working copy of each item (`name__id.gpx`). You can drop `.gpx` files here too. |
| `inbox/` | Drop `.gpx`, `.kml`, `.kmz` files here; they are imported within ~30 s (or press *Rescan folders*). Unreadable files are moved to `inbox/rejected/`. |
| `originals/` | The file exactly as you uploaded it. |
| `history/` | Undo snapshots. |
| `index.db` | Search index; it is rebuilt from `library/` if lost. |

Back up the `data` folder (e.g. with Hyper Backup). If you delete a file from `library/` in File Station it disappears from the app at the next rescan.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `APP_USER`, `APP_PASSWORD` | empty | Set **both** to require a login. |
| `MAX_UPLOAD_MB` | `50` | Largest accepted upload. |
| `RESCAN_SECONDS` | `30` | How often the library/inbox folders are checked. |
| `DATA_DIR` | `/data` | Data folder inside the container. |
| `TZ` | – | Time zone for the container. |

## Security notes

- By default there is **no login**. Keep it on your home network, or set `APP_USER` / `APP_PASSWORD`.
- To reach it from outside, put it behind DSM's **Reverse Proxy** with HTTPS (Control Panel → Login Portal → Advanced → Reverse Proxy) rather than forwarding port 8080. Basic auth over plain HTTP sends the password unencrypted.
- The container runs as root by default, so files in `data/` are owned by root. To run as your own user, add e.g. `user: "1026:100"` to the service in `docker-compose.yml` (find your IDs with `id <username>` over SSH) and make sure that user owns the `data` folder.
- The map shows tiles from openstreetmap.org / opentopomap.org, so the browser needs internet access for the background map (your files never leave the NAS).

## Things to know

- Everything is stored as GPX internally. KML/KMZ is converted on import; KML is generated on export (timestamps are kept using `gx:Track`).
- A GPX track with several segments is merged into one track; GPX routes and KML lines become tracks; KML polygons, styles and folders are not kept.
- Reversing a track removes its timestamps (they would run backwards).
- Ascent/descent uses a 3 m threshold to ignore GPS noise, so it can differ slightly from other tools.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q --ignore=tests/e2e    # backend tests (about 10 seconds)
# Leaflet is fetched during the Docker build. To run locally, fetch it once:
npm pack leaflet@1.9.4 && tar xzf leaflet-1.9.4.tgz
mkdir -p app/static/vendor/leaflet && cp -r package/dist/{leaflet.js,leaflet.css,images} app/static/vendor/leaflet/
DATA_DIR=./data uvicorn app.main:app --port 8080

# browser tests (need Leaflet from the step above)
playwright install chromium    # playwright itself comes from requirements-dev.txt
python -m pytest -q tests/e2e
```

GitHub Actions (`.github/workflows/ci.yml`) runs the backend tests (on amd64 and arm64), the browser tests, then builds the image, smoke-tests the running container on both CPU types and, on `main` and `v*` tags, pushes a multi-arch image to GHCR.
