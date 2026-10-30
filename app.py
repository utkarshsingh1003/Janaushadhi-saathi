"""Janaushadhi Saathi Flask application.

A small, server-rendered citizen information tool. No accounts, no patient
records and no prescriptions are stored. See README.md for the safety model.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from flask import (
    Flask,
    Request,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from core import db
from core.catalogue import CatalogueError
from core.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    js_strings,
    reason_label,
    translate,
)
from core.matcher import (
    Candidate,
    Query,
    find_alternatives,
    identify_text,
)
from core.medicine import (
    format_strengths,
    parse_bare_strength,
    parse_strengths,
)
from core.ocr import (
    ImageTooLarge,
    InvalidImage,
    OCRUnavailable,
    available_languages,
    extract_text,
    ocr_available,
)
from core.pricing import (
    PriceError,
    calculate_savings_list,
    compare_prices,
    format_inr,
)
from core.ratelimit import RateLimiter
from core.search import search_rows

APP_VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
FEEDBACK_CATEGORIES = {
    "incorrect_medicine",
    "outdated_kendra",
    "website_problem",
    "other",
}

LIMITER = RateLimiter()


class InMemoryUploadRequest(Request):
    """Keep multipart file streams in memory instead of Werkzeug's temp file.

    Flask's default stream factory spools uploads larger than 500 KB to a
    temporary file. OCR images must not be written to disk even transiently, so
    every upload for this app is held in a bounded (MAX_CONTENT_LENGTH) BytesIO.
    The image is discarded when the request finishes.
    """

    def _get_file_stream(
        self,
        total_content_length: int | None,
        content_type: str | None,
        filename: str | None = None,
        content_length: int | None = None,
    ):
        return io.BytesIO()


def create_app(test_config: Mapping[str, Any] | None = None) -> Flask:
    # Serverless platforms such as Vercel mount the function filesystem as
    # read-only, so Flask's default <root>/instance folder is not writable there
    # and creating it fails. Keep writable runtime state (for example the SQLite
    # database) under /tmp when running on Vercel; local development keeps the
    # default instance folder.
    instance_path = os.environ.get("FLASK_INSTANCE_PATH") or (
        "/tmp" if os.environ.get("VERCEL") == "1" else None
    )
    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=instance_path,
    )
    app.request_class = InMemoryUploadRequest
    environment = os.environ.get("FLASK_ENV", "development")
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if environment == "production":
            raise RuntimeError(
                "SECRET_KEY must be set in production. Configure it in the hosting "
                "provider's environment settings; never commit it."
            )
        secret_key = "dev-only-insecure-key"

    app.config.update(
        SECRET_KEY=secret_key,
        APP_ENV=environment,
        DATABASE=os.environ.get(
            "DATABASE_PATH", str(Path(app.instance_path) / "janaushadhi.db")
        ),
        MEDICINES_CSV=os.environ.get("MEDICINES_CSV", str(BASE_DIR / "data" / "medicines.csv")),
        KENDRAS_CSV=os.environ.get("KENDRAS_CSV", str(BASE_DIR / "data" / "kendras.csv")),
        SOURCES_JSON=os.environ.get("SOURCES_JSON", str(BASE_DIR / "data" / "sources.json")),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        OCR_MAX_BYTES=8 * 1024 * 1024,
        RATELIMIT_ENABLED=os.environ.get("RATELIMIT_ENABLED", "1") == "1",
        CSRF_ENABLED=True,
        ADMIN_TOKEN=os.environ.get("ADMIN_TOKEN", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=environment == "production",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        TRUST_PROXY=os.environ.get("TRUST_PROXY", "0") == "1",
        CATALOGUE_RELOAD=os.environ.get("CATALOGUE_RELOAD", "0") == "1",
        OCR_AVAILABLE=ocr_available(),
        OCR_LANGUAGES=available_languages(),
    )
    if test_config:
        app.config.update(test_config)
    app.json.sort_keys = False

    if app.config.get("TRUST_PROXY"):
        # Render and similar platforms terminate TLS and forward the client
        # address. Only enable this behind a proxy that overwrites these
        # headers; otherwise both HTTPS detection and rate limiting can be
        # spoofed.
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(
            app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0
        )

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    try:
        db.init_database(
            app.config["DATABASE"],
            app.config["MEDICINES_CSV"],
            app.config["KENDRAS_CSV"],
            force_reload=bool(app.config.get("CATALOGUE_RELOAD")),
        )
    except CatalogueError as exc:
        app.logger.error("Catalogue validation failed: %s", "; ".join(exc.errors[:10]))
        raise

    # Keep the stated feedback-retention window honest even when an operator
    # never runs a separate maintenance job. Only the count is logged.
    try:
        purge_connection = db.connect(app.config["DATABASE"])
        try:
            purged = db.purge_feedback(purge_connection, retention_days=365)
        finally:
            purge_connection.close()
        if purged:
            app.logger.info("Purged %d feedback records older than 365 days", purged)
    except Exception:
        # Retention cleanup must not prevent the public information site from
        # starting; an operator can still run the purge later.
        app.logger.warning("Feedback retention cleanup failed", exc_info=True)

    sources = _load_sources(Path(app.config["SOURCES_JSON"]))
    app.config["SOURCES"] = sources

    _register_hooks(app)
    _register_template_helpers(app)
    _register_pages(app)
    _register_api(app)
    _register_errors(app)
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value


def _load_sources(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        links = data.get("official_links")
        if not isinstance(links, list):
            links = []
        data["official_links"] = [
            {
                **link,
                "url": _http_url(link.get("url")),
            }
            for link in links
            if isinstance(link, dict) and _http_url(link.get("url"))
        ]
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return {"official_links": [], "medicines": {}, "kendras": {}}


def _client_ip() -> str:
    if current_app.config.get("TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_check(bucket: str, limit: int, window: int) -> int | None:
    """Return ``None`` when allowed, or the Retry-After seconds when blocked."""
    if not current_app.config.get("RATELIMIT_ENABLED"):
        return None
    allowed, retry_after = LIMITER.allow(f"{bucket}:{_client_ip()}", limit, window)
    return None if allowed else retry_after


def _limit(bucket: str, limit: int, window: int) -> Response | None:
    retry_after = _rate_check(bucket, limit, window)
    if retry_after is None:
        return None
    response = jsonify(
        {
            "ok": False,
            "error": {
                "code": "rate_limited",
                "message": "Too many requests. Please wait and try again.",
            },
        }
    )
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def _api_error(
    code: str, message: str, status: int = 400, **extra: Any
) -> tuple[Response, int]:
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    return jsonify(payload), status


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.path == "/health"


def _safe_next(target: str | None) -> str:
    if not target:
        return url_for("home")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("home")
    return target


def _csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def _check_csrf() -> None:
    if not current_app.config.get("CSRF_ENABLED"):
        return
    expected = session.get("_csrf", "")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        abort(400, description="csrf_failed")


def _medicine_public(row: Mapping[str, Any]) -> dict[str, Any]:
    """A deliberately small public representation (no internal search blob)."""
    return {
        "id": row["id"],
        "generic_name": row["generic_name"],
        "brand_names": [b.strip() for b in str(row.get("common_brands") or "").split(";") if b.strip()],
        "composition": row["composition"],
        "strength": row["strength"],
        "dosage_form": row["dosage_form"],
        "route": row["route"],
        "release_type": row["release_type"],
        "pack_size": row["pack_size"],
        "pack_unit": row["pack_unit"],
        "category": row["category"],
        "product_code": row["product_code"],
        "jan_price_paise": row.get("jan_price_paise"),
        "jan_price_verified": bool(row.get("jan_price_verified")),
        "price_verified": bool(row.get("jan_price_verified")) and not bool(row.get("is_sample")),
        "price_source": row.get("jan_price_source") or "",
        "is_sample": bool(row.get("is_sample")),
        "source_name": row.get("source_name") or "",
        "source_url": row.get("source_url") or "",
        "last_verified": row.get("last_verified") or "",
    }


def _kendra_public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kendra_code": row["kendra_code"],
        "name": row["name"],
        "owner_name": row.get("owner_name") or "",
        "address": row["address"],
        "locality": row.get("locality") or "",
        "city": row["city"],
        "district": row["district"],
        "state": row["state"],
        "pincode": row["pincode"],
        "phone": row.get("phone") or "",
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "coordinate_precision": row.get("coordinate_precision") or "unknown",
        "distance_km": row.get("distance_km"),
        "is_sample": bool(row.get("is_sample")),
        "source_name": row.get("source_name") or "",
        "last_verified": row.get("last_verified") or "",
    }


def _catalogue_candidates(connection) -> list[Candidate]:
    return [Candidate.from_row(row) for row in db.all_medicines(connection)]


def _catalogue_map(connection) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in db.all_medicines(connection)}


def _reasons(codes: list[str] | tuple[str, ...], lang: str) -> list[dict[str, str]]:
    return [{"code": code, "label": reason_label(code, lang)} for code in codes]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def _before() -> None:
        g.started_at = time.monotonic()
        requested = request.args.get("lang", "")
        if requested in SUPPORTED_LANGUAGES:
            g.lang = requested
        else:
            cookie = request.cookies.get("lang", "")
            g.lang = cookie if cookie in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
        if request.path.startswith("/static/"):
            return
        g.db = db.connect(app.config["DATABASE"])

    @app.teardown_appcontext
    def _close(_error: BaseException | None = None) -> None:
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.after_request
    def _headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(self), camera=(), microphone=(), payment=(), usb=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self'; "
            "script-src 'self'; "
            "worker-src 'self'; "
            "manifest-src 'self'; "
            "connect-src 'self'; "
            "frame-src https://www.openstreetmap.org; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'",
        )
        if request.is_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        elif request.path.startswith("/api/") or request.path == "/health":
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            response.headers.setdefault("Cache-Control", "no-cache")
        if not request.path.startswith("/static/"):
            duration_ms = int((time.monotonic() - getattr(g, "started_at", time.monotonic())) * 1000)
            # Deliberately no query string or request body: search terms and
            # prescription text must never reach application logs.
            app.logger.info("%s %s -> %s (%dms)", request.method, request.path, response.status_code, duration_ms)
        return response

    @app.before_request
    def _method_guard() -> None:
        if request.method not in {"GET", "HEAD", "OPTIONS", "POST"}:
            abort(405)


def _register_template_helpers(app: Flask) -> None:
    @app.context_processor
    def _inject() -> dict[str, Any]:
        lang = getattr(g, "lang", DEFAULT_LANGUAGE)
        try:
            data_summary = db.catalogue_summary(g.db)
        except Exception:
            data_summary = {"medicines": 0, "kendras": 0, "mode": "unknown", "loaded_at": ""}
        return {
            "lang": lang,
            "t": lambda text: translate(text, lang),
            "reason": lambda code: reason_label(code, lang),
            "csrf_token": _csrf_token,
            "js_i18n": js_strings(lang),
            "current_path": request.full_path.rstrip("?"),
            "app_version": APP_VERSION,
            "data_summary": data_summary,
        }

    @app.template_filter("inr")
    def _inr(value: int | None) -> str:
        return format_inr(value)

    @app.template_filter("strengths")
    def _strengths(value: str) -> str:
        parsed = parse_strengths(value)
        return format_strengths(parsed) if parsed else value

    @app.template_filter("formlabel")
    def _formlabel(value: str) -> str:
        return str(value or "").replace("-", " ").capitalize()

    @app.template_filter("dateonly")
    def _dateonly(value: str) -> str:
        return (value or "")[:10]

    @app.template_global("page_url")
    def _page_url(page: int) -> str:
        args = request.args.to_dict(flat=True)
        args["page"] = str(page)
        return url_for(request.endpoint or "home", **(request.view_args or {}), **args)

    @app.template_global("endpoint_active")
    def _endpoint_active(*names: str) -> bool:
        return request.endpoint in names

    @app.template_global("osm_embed")
    def _osm_embed(latitude: float, longitude: float) -> str:
        delta = 0.008
        bbox = f"{longitude - delta},{latitude - delta},{longitude + delta},{latitude + delta}"
        return (
            "https://www.openstreetmap.org/export/embed.html"
            f"?bbox={bbox}&layer=mapnik&marker={latitude},{longitude}"
        )

    @app.template_global("osm_link")
    def _osm_link(latitude: float, longitude: float) -> str:
        return f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=16/{latitude}/{longitude}"

    @app.template_global("directions_link")
    def _directions_link(latitude: float, longitude: float) -> str:
        return f"https://www.google.com/maps/dir/?api=1&destination={latitude},{longitude}"


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


def _register_pages(app: Flask) -> None:
    @app.get("/")
    def home() -> str:
        summary = db.catalogue_summary(g.db)
        return render_template(
            "index.html",
            summary=summary,
            sources=app.config.get("SOURCES", {}),
            sample_examples=["Paracetamol", "Metformin", "Amlodipine", "Dolo 650"],
        )

    @app.get("/search")
    def search() -> str:
        retry_after = _rate_check("search", 120, 60)
        if retry_after is not None:
            response = current_app.make_response(
                render_template(
                    "error.html",
                    code=429,
                    title="Too many requests",
                    message="Please wait a moment and try again.",
                )
            )
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response
        page = _safe_int(request.args.get("page"), 1, 1, 10000)
        sort = request.args.get("sort", "relevance")
        if sort not in {"relevance", "name", "price"}:
            sort = "relevance"
        result = search_rows(
            db.all_medicines(g.db),
            query=request.args.get("q", "")[:120],
            form=request.args.get("form", "")[:60],
            category=request.args.get("category", "")[:80],
            sort=sort,
            page=page,
            per_page=10,
        )
        return render_template(
            "search.html",
            result=result,
            forms=db.medicine_forms(g.db),
            categories=db.medicine_categories(g.db),
            sort=sort,
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/medicine/<medicine_id>")
    def medicine_detail(medicine_id: str) -> str:
        if not ID_RE.match(medicine_id):
            abort(404)
        row = db.medicine_by_id(g.db, medicine_id)
        if row is None:
            abort(404)
        candidates = _catalogue_candidates(g.db)
        target = Candidate.from_row(row)
        alternatives = find_alternatives(target, candidates)
        return render_template(
            "medicine.html",
            medicine=row,
            medicine_public=_medicine_public(row),
            alternatives=alternatives,
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/compare")
    def compare_index() -> str:
        page = _safe_int(request.args.get("page"), 1, 1, 10000)
        result = search_rows(
            db.all_medicines(g.db),
            query=request.args.get("q", "")[:120],
            sort="name",
            page=page,
            per_page=20,
        )
        return render_template(
            "compare_index.html",
            result=result,
            summary=db.catalogue_summary(g.db),
        )

    @app.route("/compare/<medicine_id>", methods=["GET", "POST"])
    def compare_detail(medicine_id: str) -> str:
        if not ID_RE.match(medicine_id):
            abort(404)
        row = db.medicine_by_id(g.db, medicine_id)
        if row is None:
            abort(404)

        form_values = {
            "brand_price": request.values.get("brand_price", ""),
            "brand_pack_size": request.values.get("brand_pack_size", ""),
            "quantity": request.values.get("quantity", "1"),
            "purchases_per_month": request.values.get("purchases_per_month", "1"),
        }
        comparison = None
        error = ""
        if request.method == "POST":
            _check_csrf()
            try:
                comparison = compare_prices(
                    row,
                    form_values["brand_price"],
                    form_values["brand_pack_size"] or None,
                    form_values["quantity"],
                    form_values["purchases_per_month"],
                )
            except PriceError as exc:
                error = {
                    "invalid_brand_price": "Enter a valid branded price greater than zero.",
                    "invalid_pack_size": "Pack size and quantity must be greater than zero.",
                    "invalid_quantity": "Pack size and quantity must be greater than zero.",
                    "invalid_money": "Enter a valid branded price greater than zero.",
                }.get(str(exc), "Please check the highlighted fields.")
        return render_template(
            "compare_detail.html",
            medicine=row,
            medicine_public=_medicine_public(row),
            comparison=comparison,
            form_values=form_values,
            error=error,
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/savings")
    def savings() -> str:
        return render_template("savings.html", summary=db.catalogue_summary(g.db))

    @app.get("/kendra")
    def kendra() -> str:
        filters = {
            "state": request.args.get("state", "")[:80],
            "district": request.args.get("district", "")[:80],
            "city": request.args.get("city", "")[:80],
            "pincode": request.args.get("pincode", "")[:6],
            "kendra_code": request.args.get("kendra_code", "")[:40],
            "q": request.args.get("q", "")[:120],
        }
        page = _safe_int(request.args.get("page"), 1, 1, 1000)
        per_page = 12
        result = db.search_kendras(
            g.db,
            state=filters["state"],
            district=filters["district"],
            city=filters["city"],
            pincode=filters["pincode"],
            kendra_code=filters["kendra_code"],
            query=filters["q"],
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        pages = max(1, (result["total"] + per_page - 1) // per_page)
        return render_template(
            "kendra.html",
            filters=filters,
            kendras=result["items"],
            total=result["total"],
            page=page,
            pages=pages,
            facets=db.kendra_facets(g.db),
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/ocr")
    def ocr_page() -> str:
        return render_template(
            "ocr.html",
            ocr_available=bool(app.config.get("OCR_AVAILABLE")),
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/learn")
    def learn() -> str:
        return render_template("learn.html", summary=db.catalogue_summary(g.db))

    @app.get("/resources")
    def resources() -> str:
        return render_template(
            "resources.html",
            sources=app.config.get("SOURCES", {}),
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/faq")
    def faq() -> str:
        return render_template("faq.html", summary=db.catalogue_summary(g.db))

    @app.route("/help", methods=["GET", "POST"])
    def help_page() -> str:
        feedback_sent = False
        if request.method == "POST":
            _check_csrf()
            category = request.form.get("category", "other")
            message = (request.form.get("message") or "").strip()
            contact = (request.form.get("contact") or "").strip()[:120]
            if category not in FEEDBACK_CATEGORIES:
                category = "other"
            if len(message) < 10 or len(message) > 1500:
                flash(translate("Please write at least 10 characters.", getattr(g, "lang", "en")), "error")
            else:
                reference = db.add_feedback(g.db, category, message, contact, "/help")
                db.log_audit(g.db, "feedback_created", f"id={reference}")
                feedback_sent = True
        return render_template(
            "help.html",
            feedback_sent=feedback_sent,
            feedback_categories=sorted(FEEDBACK_CATEGORIES),
            summary=db.catalogue_summary(g.db),
        )

    @app.get("/about")
    def about() -> str:
        return render_template("about.html", summary=db.catalogue_summary(g.db))

    @app.get("/privacy")
    def privacy() -> str:
        return render_template("privacy.html", summary=db.catalogue_summary(g.db))

    @app.get("/offline")
    def offline() -> str:
        return render_template("offline.html", summary=db.catalogue_summary(g.db))

    @app.get("/sw.js")
    def service_worker():
        response = send_from_directory(
            app.static_folder, "sw.js", mimetype="application/javascript"
        )
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/language/<lang>")
    def set_language(lang: str):
        if lang not in SUPPORTED_LANGUAGES:
            abort(404)
        target = _safe_next(request.args.get("next"))
        response = redirect(target)
        response.set_cookie(
            "lang",
            lang,
            max_age=60 * 60 * 24 * 365,
            samesite="Lax",
            secure=bool(app.config.get("SESSION_COOKIE_SECURE")),
            httponly=False,
        )
        return response


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


def _register_api(app: Flask) -> None:
    @app.get("/health")
    def health():
        try:
            summary = db.catalogue_summary(g.db)
            g.db.execute("SELECT 1").fetchone()
            database_status = "ok"
            status = "ok"
            code = 200
        except Exception:
            app.logger.exception("Health check database failure")
            summary = {}
            database_status = "error"
            status = "degraded"
            code = 503
        payload = {
            "status": status,
            "service": "janaushadhi-saathi",
            "version": APP_VERSION,
            "time": db.utc_now(),
            "database": {
                "status": database_status,
                "medicines": summary.get("medicines", 0),
                "kendras": summary.get("kendras", 0),
            },
            "catalogue": {
                "mode": summary.get("mode", "unknown"),
                "version": summary.get("version", ""),
                "loaded_at": summary.get("loaded_at", ""),
                "official_data": False if summary.get("mode") != "verified" else True,
            },
            "ocr": {
                "available": bool(app.config.get("OCR_AVAILABLE")),
                "languages": list(app.config.get("OCR_LANGUAGES") or []),
            },
        }
        return jsonify(payload), code

    @app.get("/api/meta")
    def api_meta():
        summary = db.catalogue_summary(g.db)
        return jsonify(
            {
                "ok": True,
                "data": {
                    **summary,
                    "official_data": summary.get("mode") == "verified",
                    "disclaimer": (
                        "Independent information tool. Not a government application. "
                        "No medical advice, diagnosis or treatment is provided."
                    ),
                },
            }
        )

    @app.get("/api/search")
    def api_search():
        limit_response = _limit("search", 120, 60)
        if limit_response:
            return limit_response
        page = _safe_int(request.args.get("page"), 1, 1, 10000)
        per_page = _safe_int(request.args.get("per_page"), 10, 1, 50)
        result = search_rows(
            db.all_medicines(g.db),
            query=request.args.get("q", "")[:120],
            form=request.args.get("form", "")[:60],
            category=request.args.get("category", "")[:80],
            sort=request.args.get("sort", "relevance")
            if request.args.get("sort") in {"relevance", "name", "price"}
            else "relevance",
            page=page,
            per_page=per_page,
        )
        return jsonify(
            {
                "ok": True,
                "query": result["query"],
                "total": result["total"],
                "page": result["page"],
                "pages": result["pages"],
                "data": [
                    {
                        **_medicine_public(item["row"]),
                        "relevance": item["score"],
                        "matched_field": item["matched_field"],
                    }
                    for item in result["entries"]
                ],
                "notice": (
                    "Search results are catalogue entries, not medical recommendations."
                ),
            }
        )

    @app.get("/api/medicines/<medicine_id>")
    def api_medicine(medicine_id: str):
        if not ID_RE.match(medicine_id):
            return _api_error("invalid_id", "Invalid medicine identifier.", 400)
        row = db.medicine_by_id(g.db, medicine_id)
        if row is None:
            return _api_error("not_found", "Medicine not found.", 404)
        target = Candidate.from_row(row)
        alternatives = find_alternatives(target, _catalogue_candidates(g.db))
        lang = getattr(g, "lang", DEFAULT_LANGUAGE)
        return jsonify(
            {
                "ok": True,
                "data": _medicine_public(row),
                "alternatives": [
                    {
                        "medicine": _medicine_public(db.medicine_by_id(g.db, candidate.id) or {}),
                        "status": result.status,
                        "reasons": _reasons(result.reasons, lang),
                        "requires_pharmacist": result.requires_pharmacist,
                    }
                    for candidate, result in alternatives
                ],
                "safety": _safety_payload(lang),
            }
        )

    @app.get("/api/alternatives/<medicine_id>")
    def api_alternatives(medicine_id: str):
        if not ID_RE.match(medicine_id):
            return _api_error("invalid_id", "Invalid medicine identifier.", 400)
        row = db.medicine_by_id(g.db, medicine_id)
        if row is None:
            return _api_error("not_found", "Medicine not found.", 404)
        target = Candidate.from_row(row)
        alternatives = find_alternatives(target, _catalogue_candidates(g.db))
        lang = getattr(g, "lang", DEFAULT_LANGUAGE)
        return jsonify(
            {
                "ok": True,
                "medicine": _medicine_public(row),
                "matches": [
                    {
                        "medicine": _medicine_public(db.medicine_by_id(g.db, candidate.id) or {}),
                        "status": result.status,
                        "reasons": _reasons(result.reasons, lang),
                        "requires_pharmacist": result.requires_pharmacist,
                    }
                    for candidate, result in alternatives
                ],
                "note": (
                    "A close catalogue match is not a substitution recommendation. "
                    "Ask a pharmacist or doctor."
                ),
            }
        )

    @app.post("/api/compare")
    def api_compare():
        limit_response = _limit("compare", 60, 60)
        if limit_response:
            return limit_response
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _api_error("invalid_json", "Send a JSON object.", 400)
        medicine_id = str(payload.get("medicine_id") or "").strip()
        if not ID_RE.match(medicine_id):
            return _api_error("invalid_medicine_id", "Invalid medicine identifier.", 400)
        row = db.medicine_by_id(g.db, medicine_id)
        if row is None:
            return _api_error("not_found", "Medicine not found.", 404)
        try:
            comparison = compare_prices(
                row,
                payload.get("brand_price"),
                payload.get("brand_pack_size"),
                payload.get("quantity", 1),
                payload.get("purchases_per_month", 1),
            )
        except PriceError as exc:
            return _api_error(
                str(exc), "Check the price, pack size and quantity values.", 422
            )
        lang = getattr(g, "lang", DEFAULT_LANGUAGE)
        return jsonify(
            {
                "ok": True,
                "medicine": _medicine_public(row),
                "comparison": comparison,
                "safety": _safety_payload(lang),
            }
        )

    @app.post("/api/savings")
    def api_savings():
        limit_response = _limit("savings", 60, 60)
        if limit_response:
            return limit_response
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return _api_error("invalid_json", "Send a JSON object with an items array.", 400)
        try:
            result = calculate_savings_list(payload["items"], _catalogue_map(g.db))
        except PriceError as exc:
            return _api_error(str(exc), "The comparison list could not be calculated.", 422)
        return jsonify({"ok": True, **result})

    @app.get("/api/kendras")
    def api_kendras():
        limit_response = _limit("kendra", 120, 60)
        if limit_response:
            return limit_response
        page = _safe_int(request.args.get("page"), 1, 1, 1000)
        per_page = _safe_int(request.args.get("per_page"), 20, 1, 50)
        result = db.search_kendras(
            g.db,
            state=request.args.get("state", "")[:80],
            district=request.args.get("district", "")[:80],
            city=request.args.get("city", "")[:80],
            pincode=request.args.get("pincode", "")[:6],
            kendra_code=request.args.get("kendra_code", "")[:40],
            query=request.args.get("q", "")[:120],
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        return jsonify(
            {
                "ok": True,
                "total": result["total"],
                "page": page,
                "data": [_kendra_public(row) for row in result["items"]],
                "notice": (
                    "Bundled Kendra records are illustrative samples, not real locations. "
                    "Use the official locator to confirm a Kendra."
                ),
            }
        )

    @app.get("/api/kendras/nearby")
    def api_kendras_nearby():
        limit_response = _limit("kendra_nearby", 30, 60)
        if limit_response:
            return limit_response
        try:
            latitude = float(request.args.get("lat", ""))
            longitude = float(request.args.get("lon", ""))
        except (TypeError, ValueError):
            return _api_error("invalid_location", "Valid lat and lon parameters are required.", 400)
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return _api_error("invalid_location", "Coordinates are out of range.", 400)
        radius = _safe_int(request.args.get("radius"), 25, 1, 100)
        rows = db.nearby_kendras(g.db, latitude, longitude, radius_km=radius, limit=30)
        return jsonify(
            {
                "ok": True,
                "radius_km": radius,
                "data": [_kendra_public(row) for row in rows],
                "notice": (
                    "Distances use city-centre sample coordinates and are approximate. "
                    "Records are not real Kendra locations."
                ),
            }
        )

    @app.post("/api/analyze")
    def api_analyze():
        limit_response = _limit("analyze", 30, 60)
        if limit_response:
            return limit_response
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _api_error("invalid_json", "Send a JSON object with a text field.", 400)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _api_error("invalid_text", "A non-empty text field is required.", 400)
        text = text.strip()[:2000]
        lang = getattr(g, "lang", DEFAULT_LANGUAGE)
        results = identify_text(text, _catalogue_candidates(g.db))
        candidates = []
        for item in results:
            candidate = item["candidate"]
            row = db.medicine_by_id(g.db, candidate.id)
            if row is None:
                continue
            candidates.append(
                {
                    "medicine": _medicine_public(row),
                    "status": "uncertain",
                    "confidence": item["confidence"],
                    "evidence": list(item["evidence"]),
                    "reasons": _reasons(item["evaluation"].reasons, lang),
                    "requires_pharmacist": True,
                }
            )
        strengths = parse_strengths(text)
        return jsonify(
            {
                "ok": True,
                "extracted": {
                    "text": text,
                    "strengths": [s.label() for s in strengths],
                    "strength_unit_inferred": (
                        not strengths and parse_bare_strength(text) is not None
                    ),
                },
                "candidates": candidates,
                "selection": None,
                "safety": _safety_payload(lang),
                "notice": (
                    "Candidates are possibilities for human review, not a finding or recommendation. "
                    "Nothing is selected automatically."
                ),
            }
        )

    @app.post("/api/ocr")
    def api_ocr():
        limit_response = _limit("ocr", 5, 60)
        if limit_response:
            return limit_response
        if not app.config.get("OCR_AVAILABLE"):
            return _api_error(
                "ocr_unavailable",
                "Text recognition is not available on this server right now.",
                503,
            )
        upload = request.files.get("image")
        if upload is None or not upload.filename:
            return _api_error("missing_image", "Choose an image to upload.", 400)
        data = upload.read(app.config["OCR_MAX_BYTES"] + 1)
        if len(data) > app.config["OCR_MAX_BYTES"]:
            return _api_error("image_too_large", "Please use an image under 8 MB.", 413)
        try:
            result = extract_text(data, upload.filename)
        except ImageTooLarge:
            return _api_error("image_too_large", "Please use an image under 8 MB.", 413)
        except InvalidImage as exc:
            code = str(exc) or "invalid_image"
            message = {
                "unsupported_type": "Unsupported file type. Use JPEG, PNG, WebP, BMP or TIFF.",
                "unreadable_image": "The image could not be read.",
                "too_small": "The image is too small to read.",
                "ocr_failed": "The image could not be read.",
            }.get(code, "The image could not be read.")
            return _api_error(code, message, 422)
        except OCRUnavailable:
            return _api_error(
                "ocr_unavailable",
                "Text recognition is not available on this server right now.",
                503,
            )
        return jsonify(
            {
                "ok": True,
                "text": result.text,
                "confidence": round(result.confidence, 1),
                "languages": list(result.languages),
                "warnings": list(result.warnings),
                "notice": (
                    "Review and correct this text. It is not saved and is not medical advice."
                ),
            }
        )

    @app.post("/api/feedback")
    def api_feedback():
        limit_response = _limit("feedback", 5, 3600)
        if limit_response:
            return limit_response
        _check_csrf()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _api_error("invalid_json", "Send a JSON object.", 400)
        category = str(payload.get("category") or "other")
        if category not in FEEDBACK_CATEGORIES:
            category = "other"
        message = str(payload.get("message") or "").strip()
        contact = str(payload.get("contact") or "").strip()[:120]
        page = str(payload.get("page") or "")[:200]
        if len(message) < 10 or len(message) > 1500:
            return _api_error(
                "invalid_message", "Please write between 10 and 1500 characters.", 422
            )
        reference = db.add_feedback(g.db, category, message, contact, page)
        db.log_audit(g.db, "feedback_created", f"id={reference}")
        return jsonify(
            {
                "ok": True,
                "reference": f"JS-{reference}",
                "notice": "Thank you. Do not send prescription details or medical records.",
            }
        )

    @app.get("/admin/feedback")
    def admin_feedback():
        token = app.config.get("ADMIN_TOKEN") or ""
        supplied = request.headers.get("X-Admin-Token", "")
        if not token or not supplied or not hmac.compare_digest(token, supplied):
            return _api_error("not_found", "Not found.", 404)
        return jsonify({"ok": True, "data": db.list_feedback(g.db, limit=200)})


def _safety_payload(lang: str) -> dict[str, Any]:
    return {
        "disclaimer": translate(
            "This is an independent information tool. It is not a government app and does not provide medical advice, diagnosis or treatment. Always follow your prescription and consult a qualified doctor or pharmacist.",
            lang,
        ),
        "substitution_not_authorised": True,
        "no_prescription_storage": True,
    }


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _register_errors(app: Flask) -> None:
    @app.errorhandler(400)
    def _bad_request(error):
        if _wants_json():
            return _api_error("bad_request", "Invalid request.", 400)
        return (
            render_template(
                "error.html",
                code=400,
                title="Invalid request.",
                message="Please check the form and try again.",
            ),
            400,
        )

    @app.errorhandler(404)
    def _not_found(error):
        if _wants_json():
            return _api_error("not_found", "Not found.", 404)
        return (
            render_template(
                "error.html",
                code=404,
                title="Page not found",
                message="The page you asked for does not exist.",
            ),
            404,
        )

    @app.errorhandler(413)
    def _too_large(error):
        if _wants_json():
            return _api_error("request_too_large", "The uploaded file is too large.", 413)
        return (
            render_template(
                "error.html",
                code=413,
                title="File too large",
                message="The uploaded file is too large.",
            ),
            413,
        )

    @app.errorhandler(429)
    def _rate_limited(error):
        if _wants_json():
            return _api_error("rate_limited", "Too many requests.", 429)
        return (
            render_template(
                "error.html",
                code=429,
                title="Too many requests",
                message="Please wait a moment and try again.",
            ),
            429,
        )

    @app.errorhandler(500)
    def _server_error(error):
        if _wants_json():
            return _api_error("server_error", "An unexpected error occurred.", 500)
        return (
            render_template(
                "error.html",
                code=500,
                title="Something went wrong",
                message="An unexpected error occurred. Please try again.",
            ),
            500,
        )


if __name__ == "__main__":
    create_app().run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
