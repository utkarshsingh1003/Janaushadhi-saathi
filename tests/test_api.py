"""HTTP page, JSON API and safety-behaviour tests."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

import app as app_module
from core.ocr import ImageTooLarge, InvalidImage

ROOT = Path(__file__).resolve().parent.parent


def test_health_reports_sample_mode_and_no_official_claim(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["service"] == "janaushadhi-saathi"
    assert payload["database"]["status"] == "ok"
    assert payload["database"]["medicines"] == 54
    assert payload["database"]["kendras"] == 15
    assert payload["catalogue"]["mode"] == "sample"
    assert payload["catalogue"]["official_data"] is False
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_meta_search_and_medicine_api_do_not_expose_internal_blobs(client):
    meta = client.get("/api/meta").get_json()
    assert meta["ok"] is True
    assert meta["data"]["official_data"] is False
    assert meta["data"]["mode"] == "sample"
    assert "Not a government" in meta["data"]["disclaimer"]

    search = client.get("/api/search?q=paracetamol").get_json()
    assert search["ok"] is True
    assert search["total"] >= 1
    item = search["data"][0]
    assert "search_blob" not in item
    assert item["is_sample"] is True
    assert any("not medical recommendations" in search["notice"] for _ in [0])

    medicine = client.get("/api/medicines/paracetamol-500-tablet").get_json()
    assert medicine["ok"] is True
    assert medicine["data"]["id"] == "paracetamol-500-tablet"
    assert medicine["data"]["price_verified"] is False
    assert medicine["safety"]["substitution_not_authorised"] is True
    assert all(
        alternate["status"] in {"exact", "compatible"}
        for alternate in medicine["alternatives"]
    )


def test_medicine_api_rejects_bad_ids_and_unknown_records(client):
    assert client.get("/api/medicines/../../etc/passwd").status_code == 404
    assert client.get("/api/medicines/Bad_ID").status_code == 400
    missing = client.get("/api/medicines/not-in-catalogue")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "not_found"


def test_analyze_returns_only_uncertain_candidates_and_no_selection(client):
    response = client.post("/api/analyze", json={"text": "Tab Dolo 650"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["selection"] is None
    assert payload["candidates"]
    assert all(candidate["status"] == "uncertain" for candidate in payload["candidates"])
    assert all(candidate["requires_pharmacist"] is True for candidate in payload["candidates"])
    assert any(
        candidate["medicine"]["id"] == "paracetamol-650-tablet"
        for candidate in payload["candidates"]
    )
    assert "recommendation" not in payload
    assert payload["safety"]["substitution_not_authorised"] is True


def test_analyze_validates_input(client):
    assert client.post("/api/analyze", json={}).status_code == 400
    assert client.post("/api/analyze", json={"text": "   "}).status_code == 400
    assert client.post("/api/analyze", data="not-json", content_type="text/plain").status_code == 400


def test_compare_api_calculates_with_sample_warning(client):
    response = client.post(
        "/api/compare",
        json={
            "medicine_id": "paracetamol-500-tablet",
            "brand_price": "30.00",
            "brand_pack_size": "10",
            "quantity": 10,
            "purchases_per_month": 2,
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    comparison = payload["comparison"]
    assert comparison["difference_paise"] == 2600
    assert comparison["price_verified"] is False
    assert any("not an official MRP" in warning for warning in comparison["warnings"])
    assert payload["medicine"]["is_sample"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"medicine_id": "not-real", "brand_price": "10"},
        {"medicine_id": "paracetamol-500-tablet", "brand_price": "0"},
        {"medicine_id": "paracetamol-500-tablet", "brand_price": "10", "quantity": 0},
    ],
)
def test_compare_api_rejects_invalid_payloads(client, payload):
    response = client.post("/api/compare", json=payload)
    assert response.status_code in {400, 404, 422}
    assert response.get_json()["ok"] is False


def test_savings_api_reports_totals_and_partial_failures(client):
    response = client.post(
        "/api/savings",
        json={
            "items": [
                {
                    "medicine_id": "paracetamol-500-tablet",
                    "brand_price": "30",
                    "brand_pack_size": "10",
                    "quantity": 10,
                },
                {"medicine_id": "not-real", "brand_price": "5"},
            ]
        },
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["valid_items"] == 1
    assert payload["items"][1]["ok"] is False
    assert payload["items"][1]["error"] == "unknown_medicine"
    assert payload["totals"]["difference_paise"] == 2600

    empty = client.post("/api/savings", json={"items": []})
    assert empty.status_code == 422
    assert empty.get_json()["error"]["code"] == "empty_list"


def test_kendra_api_is_sample_labelled_and_filterable(client):
    response = client.get("/api/kendras?state=Delhi")
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["total"] == 1
    record = payload["data"][0]
    assert record["is_sample"] is True
    assert record["phone"] == ""
    assert "not real locations" in payload["notice"]
    assert client.get("/api/kendras?state=Nowhere").get_json()["total"] == 0


def test_nearby_api_validates_coordinates(client):
    good = client.get("/api/kendras/nearby?lat=28.6139&lon=77.2090&radius=5")
    assert good.status_code == 200
    payload = good.get_json()
    assert payload["data"]
    assert payload["data"][0]["city"] == "New Delhi"
    assert payload["data"][0]["distance_km"] == 0.0

    assert client.get("/api/kendras/nearby?lat=999&lon=0").status_code == 400
    assert client.get("/api/kendras/nearby?lat=x&lon=0").status_code == 400
    assert client.get("/api/kendras/nearby?lat=0&lon=999").status_code == 400


def test_feedback_requires_csrf_and_stores_a_reference(client, csrf_token):
    missing = client.post("/api/feedback", json={"message": "A valid report message."})
    assert missing.status_code == 400
    assert missing.get_json()["error"]["code"] == "bad_request"

    short = client.post(
        "/api/feedback",
        json={"message": "short"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert short.status_code == 422

    good = client.post(
        "/api/feedback",
        json={"category": "website_problem", "message": "A valid report message."},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert good.status_code == 200
    payload = good.get_json()
    assert payload["ok"] is True
    assert payload["reference"].startswith("JS-")


def test_html_feedback_form_rejects_missing_csrf_and_accepts_valid_token(client, csrf_token):
    missing = client.post("/help", data={"message": "A valid report message."})
    assert missing.status_code == 400

    good = client.post(
        "/help",
        data={
            "csrf_token": csrf_token,
            "category": "website_problem",
            "message": "A valid report message.",
        },
    )
    assert good.status_code == 200
    assert b"Thank you" in good.data


def test_admin_feedback_is_404_without_a_configured_and_supplied_token(app, client):
    assert client.get("/admin/feedback").status_code == 404
    app.config["ADMIN_TOKEN"] = "operator-secret"
    assert client.get("/admin/feedback").status_code == 404
    assert (
        client.get(
            "/admin/feedback", headers={"X-Admin-Token": "wrong"}
        ).status_code
        == 404
    )
    response = client.get(
        "/admin/feedback", headers={"X-Admin-Token": "operator-secret"}
    )
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_upload_streams_are_kept_in_memory(app):
    assert app.request_class is app_module.InMemoryUploadRequest
    request = app_module.InMemoryUploadRequest({})
    stream = request._get_file_stream(2_000_000, "multipart/form-data", "label.png", None)
    assert isinstance(stream, io.BytesIO)


def test_ocr_api_degrades_gracefully_when_tesseract_is_missing(client):
    response = client.post("/api/ocr")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "ocr_unavailable"


def test_ocr_api_validates_uploads_when_available(app, client, monkeypatch):
    app.config["OCR_AVAILABLE"] = True
    assert client.post("/api/ocr").status_code == 400

    def raise_invalid(*args, **kwargs):
        raise InvalidImage("unsupported_type")

    monkeypatch.setattr(app_module, "extract_text", raise_invalid)
    response = client.post(
        "/api/ocr",
        data={"image": (io.BytesIO(b"fake"), "label.pdf")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "unsupported_type"

    def raise_large(*args, **kwargs):
        raise ImageTooLarge("too_large")

    monkeypatch.setattr(app_module, "extract_text", raise_large)
    response = client.post(
        "/api/ocr",
        data={"image": (io.BytesIO(b"fake"), "label.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "image_too_large"


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/", "Janaushadhi Saathi"),
        ("/search", "Search the medicine catalogue"),
        ("/medicine/paracetamol-500-tablet", "Paracetamol"),
        ("/compare", "Compare a branded medicine"),
        ("/compare/paracetamol-500-tablet", "Calculate comparison"),
        ("/savings", "Savings calculator"),
        ("/kendra", "illustrative samples"),
        ("/ocr", "Read a medicine package"),
        ("/learn", "Medicine safety"),
        ("/resources", "Official PMBJP resources"),
        ("/faq", "Frequently asked questions"),
        ("/help", "Help and feedback"),
        ("/about", "About Janaushadhi Saathi"),
        ("/privacy", "Privacy and data"),
        ("/offline", "No internet connection"),
    ],
)
def test_all_pages_render(client, path, needle):
    response = client.get(path)
    assert response.status_code == 200
    assert needle.encode() in response.data


def test_404_and_405_responses(client):
    page = client.get("/no-such-page")
    assert page.status_code == 404
    assert b"Page not found" in page.data
    api = client.get("/api/no-such-endpoint")
    assert api.status_code == 404
    assert api.get_json()["ok"] is False
    assert api.get_json()["error"]["code"] == "not_found"
    assert client.delete("/api/analyze").status_code == 405


def test_language_switch_persists_cookie_and_translates_page(client):
    response = client.get("/language/hi?next=/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert "lang=hi" in response.headers["Set-Cookie"]

    home = client.get("/")
    assert home.status_code == 200
    assert "जनऔषधि साथी".encode() in home.data
    assert 'lang="hi"' in home.get_data(as_text=True)
    assert "कैटलॉग संस्करण".encode() not in home.data  # sample branch has no version line

    assert client.get("/language/en?next=/").status_code == 302
    assert 'lang="en"' in client.get("/").get_data(as_text=True)


def test_open_redirect_targets_are_rejected(client):
    response = client.get("/language/hi?next=https://evil.example/")
    assert response.headers["Location"] == "/"
    response = client.get("/language/hi?next=//evil.example/")
    assert response.headers["Location"] == "/"


def test_security_headers_and_csp(client):
    response = client.get("/")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=(self)" in response.headers["Permissions-Policy"]
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert response.headers["Cache-Control"] == "no-cache"

    secure = client.get("/", base_url="https://localhost")
    assert "max-age=31536000" in secure.headers["Strict-Transport-Security"]


def test_proxy_fix_sets_hsts_only_when_trusted(tmp_path):
    trusted = app_module.create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(tmp_path / "trusted.db"),
            "RATELIMIT_ENABLED": False,
            "TRUST_PROXY": True,
            "OCR_AVAILABLE": False,
        }
    )
    response = trusted.test_client().get(
        "/", base_url="http://localhost", headers={"X-Forwarded-Proto": "https"}
    )
    assert "Strict-Transport-Security" in response.headers

    direct = app_module.create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(tmp_path / "direct.db"),
            "RATELIMIT_ENABLED": False,
            "TRUST_PROXY": False,
            "OCR_AVAILABLE": False,
        }
    )
    response = direct.test_client().get(
        "/", base_url="http://localhost", headers={"X-Forwarded-Proto": "https"}
    )
    assert "Strict-Transport-Security" not in response.headers


def test_csp_blocks_inline_handlers_in_templates(client):
    html = client.get("/medicine/paracetamol-500-tablet").get_data(as_text=True)
    assert "onclick=" not in html.lower()
    assert "onload=" not in html.lower()
    assert "<style" not in html.lower()


def test_service_worker_is_served_with_scope_and_no_cache(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert response.headers["Cache-Control"] == "no-cache"
    assert "CACHE_NAME" in response.get_data(as_text=True)


def test_manifest_and_icons_are_served(client):
    manifest = client.get("/static/manifest.webmanifest")
    assert manifest.status_code == 200
    data = json.loads(manifest.get_data(as_text=True))
    assert data["name"] == "Janaushadhi Saathi"
    assert client.get("/static/icon-192.png").status_code == 200
    assert client.get("/static/icon-512.png").status_code == 200
