# Janaushadhi Saathi

An independent, bilingual (English/Hindi) citizen-information tool for affordable
generic medicines, cautious price comparisons and Jan Aushadhi Kendra discovery.

> This is **not** a government application and is not affiliated with PMBJP,
> PMBI or any government department. It does not provide medical advice,
> diagnosis, treatment, prescriptions or medicine ordering.

## What this build does

- Searches a validated medicine catalogue by generic name, salt, brand,
  strength, dosage form, category or product code (typo-tolerant ranking).
- Shows composition, strength, form, route, release type, pack size, price
  status, source and freshness for each record.
- Compares a user-entered branded price with a catalogue generic price,
  normalising both prices to a cost per unit and showing every assumption.
- Keeps a temporary multi-medicine savings list **in the browser only**.
- Finds sample Kendra records by state, district, city, PIN code or Kendra code
  and can sort illustrative records by approximate distance.
- Extracts text from a clear image **in memory** with Tesseract, then asks the
  strict matcher for uncertain catalogue candidates that a human must review.
- Links to official PMBJP resources instead of copying information that may
  become stale.
- Works without an account, without patient records and without third-party
  analytics.

## Safety model

The matching engine in `core/matcher.py` never recommends a substitution. It
answers a narrower question: does this catalogue record have the same active
ingredient(s), numeric strength, dosage form, route and release mechanism?

- Generic name is checked **before** any brand claim.
- Composition is compared as an order-insensitive set of canonical ingredients.
- Combination products require one explicit strength per ingredient and reject
  partial ingredient overlap.
- Numeric strength mismatches are hard rejections. `0.5 g` and `500 mg` are
  equivalent; a missing or inferred strength stays uncertain.
- A different dosage form is rejected; a different variant (for example
  dispersible versus film-coated) is uncertain and needs pharmacist review.
- Immediate-release and modified-release products are never interchangeable.
  Two different modified-release mechanisms are uncertain, not equal.
- Missing route, release type or composition can never produce an exact match.
- Brand names are informational only and cannot override composition checks.
- Fuzzy search lives only in `core/search.py`. It can rank results but can never
  establish compatibility.
- `/api/analyze` always returns `selection: null`; candidates are marked
  `uncertain`, carry reasons, and require pharmacist review.
- No autonomous substitution advice is generated anywhere.

Every catalogue record shown by this build is explicitly sample data. Prices are
integer paise and are marked unverified; no sample price is presented as an
official MRP, product identifier, Kendra location, phone number or coordinate.

## Quick start

Requirements: Python 3.12+ (locally verified on 3.14.6), pip. Tesseract is
optional for local development; without it the OCR endpoint returns a clear
`503` and the rest of the site still works.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in values you need; never commit .env
python app.py               # http://127.0.0.1:5000
```

For a production-style local run:

```bash
FLASK_ENV=production SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  .venv/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 --threads 4 wsgi:app
```

`FLASK_ENV=production` refuses to start without `SECRET_KEY`. The SQLite
database is created under `instance/` and rebuilt from the bundled validated
CSVs when their hash changes.

## Tests

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m pytest --cov=app --cov=core --cov=scripts --cov-report=term-missing
```

Actual local result on 2026-10-30 (Python 3.14.6):

- **149 tests passed**
- **86% total statement coverage** across `app.py`, `core/` and `scripts/`
- No Docker, GitHub CLI, Tesseract or Render credentials were available locally,
  so the Docker image, GitHub Actions run and Render deployment were **not**
  executed. The configuration files are provided; see [DEPLOYMENT.md](DEPLOYMENT.md).
- The GitHub token available in this environment could not create a repository
  (`403 Resource not accessible by personal access token`) and the `gh` CLI is
  absent, so no GitHub repository was created or pushed. See
  [DEPLOYMENT.md](DEPLOYMENT.md) for the exact steps and blockers.

The tests cover strict matcher regressions, combination handling, strength and
release rejections, pack-price arithmetic, savings-list partial failures, Kendra
filters/nearby distance, OCR validation/failure paths, CSV provenance, import
CLI atomic writes, CSRF, security headers, language persistence and sample-data
honesty.

## Docker

```bash
docker build -t janaushadhi-saathi .
docker run --rm -p 8000:8000 \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  janaushadhi-saathi
curl http://127.0.0.1:8000/health
```

The image installs `tesseract-ocr`, `tesseract-ocr-eng` and `tesseract-ocr-hin`,
runs as a non-root user, and starts Gunicorn on `$PORT` (default `8000`). No
secret is baked into the image. SQLite is rebuilt at startup; the filesystem is
ephemeral on Render, so feedback submissions do not survive a redeploy unless a
persistent disk is added.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `FLASK_ENV` | `development` | `production` requires `SECRET_KEY` and enables secure session cookies |
| `SECRET_KEY` | insecure dev fallback | Session signing/CSRF secret; required in production |
| `DATABASE_PATH` | `instance/janaushadhi.db` | SQLite path; rebuilt from CSVs on change |
| `MEDICINES_CSV` | `data/medicines.csv` | Validated medicine catalogue |
| `KENDRAS_CSV` | `data/kendras.csv` | Validated Kendra catalogue |
| `SOURCES_JSON` | `data/sources.json` | Official links and provenance text |
| `RATELIMIT_ENABLED` | `1` | Per-process in-memory rate limiting |
| `TRUST_PROXY` | `0` | Trust `X-Forwarded-For` only behind a trusted proxy |
| `CATALOGUE_RELOAD` | `0` | Force a rebuild on every startup |
| `ADMIN_TOKEN` | empty | Enables `GET /admin/feedback`; empty keeps it 404 |
| `PORT` | `8000` in Docker | Gunicorn bind port |
| `WEB_CONCURRENCY` | `2` in Docker | Gunicorn workers |
| `WEB_THREADS` | `4` in Docker | Gunicorn threads |

## JSON API

All API errors are JSON with `{"ok": false, "error": {"code": ..., "message": ...}}`.
`/health` and `/api/*` responses are `no-store`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | Liveness, DB counts, sample/verified mode, OCR availability |
| GET | `/api/meta` | Catalogue metadata and safety disclaimer |
| GET | `/api/search?q=...` | Ranked catalogue entries (`page`, `per_page` ≤ 50) |
| GET | `/api/medicines/<id>` | Public record plus strict catalogue alternatives |
| GET | `/api/alternatives/<id>` | Strict alternatives only; never a recommendation |
| POST | `/api/compare` | `{medicine_id, brand_price, brand_pack_size?, quantity?, purchases_per_month?}` |
| POST | `/api/savings` | `{items: [{medicine_id, brand_price, ...}]}`; partial errors reported |
| GET | `/api/kendras?state=...` | Sample Kendra records; filters and pagination |
| GET | `/api/kendras/nearby?lat=...&lon=...&radius=25` | Approximate sample distances |
| POST | `/api/analyze` | `{text: "..."}`; uncertain candidates only, `selection: null` |
| POST | `/api/ocr` | Multipart `image`; processed in memory, never persisted |
| POST | `/api/feedback` | Requires `X-CSRF-Token`; no prescription/medical data |
| GET | `/admin/feedback` | Requires `X-Admin-Token`; 404 unless configured |

Example:

```bash
curl -s http://127.0.0.1:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"Tab Dolo 650"}'
```

The response contains sample evidence and `uncertain` candidates. It never
contains a chosen medicine, a recommendation or permission to substitute.

## Language, privacy and security

- Language is selected with a small `lang` cookie; all interface strings have
  Hindi translations, with English fallback rather than raw keys.
- No login, accounts, patient records, prescriptions, diagnosis, orders or
  payments exist.
- OCR images are read into memory, validated for type/size/pixels, preprocessed
  and discarded. OCR text is never logged or stored.
- Application logs contain method, path, status and duration only — no query
  strings, form bodies or OCR text.
- CSRF protection covers the feedback form and feedback API. Other stateless
  JSON endpoints do not rely on cookies.
- Security headers include a strict CSP, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy`, `Permissions-Policy`, HSTS over HTTPS and no-store API
  responses.
- Rate limits are intentionally described as per-process only; use a shared
  store if you need strict multi-instance quotas.
- The savings list is stored in `localStorage`; clearing browser data removes it.

## Data provenance and refresh

`data/medicines.csv`, `data/kendras.csv` and `data/sources.json` are bundled
illustrative samples. All medicine and Kendra rows set `is_sample=1`. Prices
set `jan_price_verified=0`; Kendra names explicitly say they are not real
locations. Coordinates are city-centre approximations used only for distance
sorting. No source page was scraped.

To replace the catalogue, validate first (the default is a dry run):

```bash
python scripts/import_catalogue.py \
  --medicines /path/to/medicines.csv \
  --kendras /path/to/kendras.csv \
  --source-name "Dataset title" \
  --source-url "https://example.gov.in/source" \
  --retrieved 2026-10-30
```

Apply only after reviewing the validation output:

```bash
python scripts/import_catalogue.py ... --apply
```

The CLI reuses the startup validators, requires per-row provenance on every
non-sample row, writes canonical CSV columns, backs up existing files beside
them, replaces files atomically, rebuilds SQLite (unless `--no-rebuild`) and
appends an audit entry to `data/import_log.jsonl`. It never stores patient,
prescription or OCR data. To update `data/sources.json`, edit it manually with
the new source documentation and re-run the import.

## Project layout

```
app.py                     Flask factory, pages, JSON APIs, headers, errors
wsgi.py                    Gunicorn entry point
core/
  matcher.py               Strict safety-critical matching
  medicine.py              Strength/composition/form/route/release parsing
  search.py                Typo-tolerant discovery only
  pricing.py               Integer-paise comparison and savings arithmetic
  ocr.py                   In-memory image validation and Tesseract wrapper
  catalogue.py             CSV schema and provenance validation
  db.py                    SQLite schema, loading, Kendra queries, feedback
  ratelimit.py             Per-process limiter
  i18n.py                  English/Hindi strings and reason labels
templates/                 Server-rendered Jinja pages
static/                    CSS, JavaScript, PWA manifest, service worker, icons
data/                      Sample CSVs and official-link metadata
scripts/import_catalogue.py Validated, audited catalogue import CLI
tests/                     Pytest regression and API tests
Dockerfile / render.yaml   Deployment configuration
.github/workflows/ci.yml   CI: tests plus Docker build and health smoke test
```

## Known limitations

- The bundled catalogue is sample data, not an official PMBJP product list.
- Prices, stock, opening hours and Kendra details are not real-time.
- OCR requires Tesseract on the host; it is not installed in this local
  environment, so only the graceful-degradation path was exercised locally.
- SQLite is not suitable for horizontal scaling; use one instance or migrate to
  a shared database before adding workers/instances that write feedback.
- The rate limiter is per process. The admin feedback view is intentionally
  minimal and token-protected.
- Docker build, GitHub Actions execution and Render deployment were not run
  locally: no container runtime, `gh`, or Render credentials were available, and
  the GitHub token in use could not create repositories
  (`403 Resource not accessible by personal access token`). See
  [DEPLOYMENT.md](DEPLOYMENT.md) for the exact steps and blockers.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for local Gunicorn, Docker, GitHub Actions and
Render instructions, including the free-plan/ephemeral-storage caveats.
