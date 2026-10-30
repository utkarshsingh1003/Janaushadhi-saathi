# Deployment and verification

This document covers local production-style runs, Docker, GitHub Actions and
Render for Janaushadhi Saathi. It also records what was actually executed in
this workspace and what was blocked by missing local tools or credentials.

> The bundled catalogue is **sample data**. A successful deployment does not
> make its prices, Kendra records, product codes or coordinates official. The
> application reports `catalogue.mode` and `official_data` explicitly.

## Status at the time of this build

| Check | Result |
| --- | --- |
| Python tests | `149 passed` locally on Python 3.14.6 |
| Coverage | `86%` across `app.py`, `core/` and `scripts/` |
| Local Gunicorn smoke test | Verified locally over HTTP |
| Docker image build and container run | **Not executed** — `docker` is not installed in this workspace |
| GitHub Actions execution | **Not executed locally** — use the workflow in `.github/workflows/ci.yml`; the local machine has no `gh` CLI |
| Render deployment | **Not executed** — no Render account session, API key or CLI is available here |
| Tesseract success path | **Not executed locally** — `tesseract` is not installed here; the image includes it. The graceful `503` path and mocked OCR paths are tested. |

The configuration files are complete, but a configuration file is not proof of
a successful remote deployment. Complete the steps below with your own
credentials and record the resulting URL and CI run.

## 1. Local development

Requirements: Python 3.12 or newer and pip. Tesseract is optional locally.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python app.py
```

Open <http://127.0.0.1:5000>. Development mode uses an insecure fallback secret
when `SECRET_KEY` is empty, which is intentional for local-only use.

### Optional local OCR

Install Tesseract with English and Hindi data, then restart the app:

```bash
# macOS with Homebrew
brew install tesseract tesseract-lang

# Debian/Ubuntu
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin
```

If Tesseract is unavailable, `/api/ocr` returns a clear `503` and text can still
be pasted into the analysis box. Uploaded images are held in a bounded
`BytesIO` and are not written to application storage.

## 2. Local production-style run

```bash
export FLASK_ENV=production
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
./.venv/bin/gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 4 wsgi:app
```

`FLASK_ENV=production` refuses to start without `SECRET_KEY`. Production mode
also sets secure session cookies, so test through HTTPS or with a TLS
terminating local proxy if you need cookie-backed feedback/CSRF flows. Health
and stateless JSON endpoints work over plain HTTP for smoke testing.

Smoke checks:

```bash
curl --fail --silent http://127.0.0.1:8000/health | python3 -m json.tool

curl --fail --silent http://127.0.0.1:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Tab Dolo 650"}' | python3 -m json.tool
```

Expected safety properties:

- `/health` returns `200`, `catalogue.mode` is `sample`, and
  `catalogue.official_data` is `false` for the bundled data.
- `/api/analyze` returns `"selection": null`, candidate status `"uncertain"`,
  and a `requires_pharmacist` flag. It never returns a chosen medicine.

## 3. Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `FLASK_ENV` | Production: `production` | Enables production checks and secure cookies |
| `SECRET_KEY` | Yes in production | Session/CSRF signing secret. Generate a long random value; never commit it |
| `DATABASE_PATH` | No | SQLite path. Default: `instance/janaushadhi.db` |
| `MEDICINES_CSV` | No | Validated medicine catalogue. Default: `data/medicines.csv` |
| `KENDRAS_CSV` | No | Validated Kendra catalogue. Default: `data/kendras.csv` |
| `SOURCES_JSON` | No | Official-link/provenance metadata. Default: `data/sources.json` |
| `RATELIMIT_ENABLED` | No | `1` by default; per-process in-memory limits |
| `TRUST_PROXY` | Render: `1` | Trust one proxy hop for client IP and HTTPS detection. Use only behind a proxy that overwrites forwarded headers |
| `CATALOGUE_RELOAD` | No | `1` forces a catalogue rebuild on every startup |
| `ADMIN_TOKEN` | No | Enables `GET /admin/feedback`. Empty keeps it a 404 |
| `PORT` | Docker | Gunicorn bind port, default `8000` |
| `WEB_CONCURRENCY` | Docker | Gunicorn workers, default `2`; use `1` with SQLite and per-process limits |
| `WEB_THREADS` | Docker | Gunicorn threads, default `4` |

Do not put `SECRET_KEY`, `ADMIN_TOKEN` or any future database credential in
client-side JavaScript, the repository, a Docker image layer or a public build
log. Rotate the secret if it is exposed; rotating invalidates existing sessions.

## 4. Docker

```bash
docker build -t janaushadhi-saathi .

docker run --rm \
  --name janaushadhi-saathi \
  --publish 8000:8000 \
  --env SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  janaushadhi-saathi

curl --fail --silent http://127.0.0.1:8000/health
curl --fail --silent http://127.0.0.1:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Tab Dolo 650"}'
```

The image:

- Uses `python:3.12-slim`.
- Installs `tesseract-ocr`, `tesseract-ocr-eng` and `tesseract-ocr-hin`.
- Runs as a non-root user (`uid 10001`).
- Does not bake in a secret or a database.
- Rebuilds SQLite from the bundled validated CSVs at startup.
- Exposes a Docker `HEALTHCHECK` against `/health`.

The container filesystem is ephemeral unless a volume is mounted. Feedback is
not patient data, but it will still disappear on redeploy. If feedback must
survive, mount a writable volume and set `DATABASE_PATH` to that location; do
not assume an ephemeral Render filesystem persists it.

## 5. GitHub Actions CI

The workflow is `.github/workflows/ci.yml`. It runs on pushes to `main`, pull
requests and manual dispatch.

Jobs:

1. **Tests (Python 3.12)** — installs `requirements-dev.txt` and runs
   `python -m pytest -q`.
2. **Docker build and health smoke test** — builds the image, starts a
   container, waits for `/health`, checks sample mode and database counts, then
   confirms `/api/analyze` returns `selection: null` and only `uncertain`
   candidates.

With the GitHub CLI available:

```bash
gh repo create janaushadhi-saathi --private --source=. --remote=origin
git add .
git commit -m "Build Janaushadhi Saathi"
git push -u origin main
gh run list --workflow=ci.yml
gh run watch
```

Without `gh`, create the repository in the GitHub web UI, add it as `origin`,
push, and open the **Actions** tab. The workflow needs no secrets for the test
and Docker jobs. Pull requests from forks receive a read-only `GITHUB_TOKEN`,
which is sufficient here.

If Docker is unavailable on the runner, the Docker job cannot pass; that is a
runner problem, not a substitute for a test result. GitHub-hosted
`ubuntu-latest` runners normally provide Docker.

## 6. Render

### Option A: Blueprint (`render.yaml`)

1. Push the repository to GitHub.
2. In Render, choose **New → Blueprint**.
3. Connect the repository and select the branch containing `render.yaml`.
4. Apply the blueprint. Render creates a Docker web service with:
   - health check `/health`
   - `FLASK_ENV=production`
   - a generated `SECRET_KEY`
   - `WEB_CONCURRENCY=1`, `WEB_THREADS=4`
   - `RATELIMIT_ENABLED=1`
   - `TRUST_PROXY=1`
5. Wait for the deploy to become live.
6. Open the service URL and check `/health`.

### Option B: Manual web service

- Environment: **Docker**
- Repository: your GitHub repository
- Dockerfile path: `./Dockerfile`
- Health check path: `/health`
- Plan: **Free** (or a plan you explicitly approve)
- Environment variables:
  - `FLASK_ENV=production`
  - `SECRET_KEY` = a long random value generated by Render or by
    `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`
  - `TRUST_PROXY=1`
  - `RATELIMIT_ENABLED=1`
  - `WEB_CONCURRENCY=1`
  - `WEB_THREADS=4`

### Render verification

Replace `SERVICE_URL` with the actual URL:

```bash
curl --fail --silent "$SERVICE_URL/health" | python3 -m json.tool
curl --fail --silent "$SERVICE_URL/api/analyze" \
  -H 'Content-Type: application/json' \
  -d '{"text":"Tab Dolo 650"}' | python3 -m json.tool
```

Record the actual HTTP status, deploy ID/commit, timestamp and any failure in
your handover. Do not describe a blueprint as deployed until Render reports a
live service and the health check passes.

### Free-plan and SQLite caveats

- Render's free web services can spin down when idle; the first request after
  sleep is slower.
- The filesystem is ephemeral. The SQLite database is rebuilt from the bundled
  CSVs on each start. Feedback submissions do not survive a redeploy unless a
  persistent disk is attached and `DATABASE_PATH` points at it.
- Use one Gunicorn worker with SQLite unless you have tested concurrent writes.
- The rate limiter is per process. Multiple instances do not share quotas.
- The bundled catalogue is sample data. No live price, stock, Kendra or
  government integration exists.

## 7. Troubleshooting

| Symptom | Check |
| --- | --- |
| App exits at startup | In production, `SECRET_KEY` must be set and non-empty |
| `/health` returns `503` | SQLite path is writable and the catalogue validated; inspect container logs |
| OCR returns `503` | Tesseract executable and requested languages are installed; run `tesseract --list-langs` inside the container |
| OCR returns `422` | File type/size/pixel limits or image decoding failed; use a clear JPEG/PNG under 8 MB |
| Feedback/CSRF fails behind a proxy | Set `TRUST_PROXY=1` only when the proxy overwrites `X-Forwarded-*`; ensure HTTPS so secure session cookies are sent |
| No HSTS header | HSTS is only sent on requests detected as HTTPS; terminate TLS correctly and trust exactly one proxy hop |
| Kendra/medicine data looks stale | Check `/health` `catalogue.loaded_at`; replace data with `scripts/import_catalogue.py`, then rebuild/redeploy |
| `database is locked` | Use one worker for writes; keep SQLite local or migrate to a shared database before scaling |

## 8. Security and privacy operations

- No accounts, patient records, diagnosis, prescriptions, payments, ordering or
  stock claims are implemented.
- OCR uploads are held in memory in this application, validated for type, byte
  size and pixel count, and never written to application storage. Application
  logs contain method, path, status and duration only.
- Feedback is plain text in SQLite, intended to be retained for at most 365
  days; startup cleanup deletes older entries. Do not collect prescriptions or
  medical records.
- `GET /admin/feedback` is unavailable unless `ADMIN_TOKEN` is set. Send the
  token in `X-Admin-Token`; do not put it in a URL, HTML, JavaScript or logs.
- The CSP allows only same-origin scripts/styles and the OpenStreetMap embed
  frame. Do not add inline scripts or third-party analytics.
- Replace the sample catalogue only with a dataset whose source, retrieval date
  and permitted use you have checked. The import CLI rejects non-sample rows
  without provenance.

## 9. Reproducibility notes

- Python tested locally: 3.14.6. CI targets Python 3.12.
- Test command: `./.venv/bin/python -m pytest -q`.
- Coverage command: `./.venv/bin/python -m pytest --cov=app --cov=core --cov=scripts --cov-report=term-missing`.
- The test suite uses temporary SQLite files and never touches `instance/janaushadhi.db`.
- Docker, GitHub Actions and Render were not executed in this workspace because
  the corresponding local tools/credentials were unavailable. The provided
  configuration is ready for an operator who has them.
