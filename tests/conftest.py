"""Shared pytest fixtures.

Tests never touch the real instance database: each test app gets a temporary
SQLite file and the bundled validated sample catalogue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(tmp_path / "test.db"),
            "RATELIMIT_ENABLED": False,
            "CSRF_ENABLED": True,
            "ADMIN_TOKEN": "",
            "OCR_AVAILABLE": False,
            "CATALOGUE_RELOAD": True,
        }
    )
    yield application
    # The application closes request connections in teardown; nothing else
    # needs cleanup because SQLite lives under pytest's tmp_path.


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def csrf_token(client):
    response = client.get("/help")
    assert response.status_code == 200
    with client.session_transaction() as session:
        token = session.get("_csrf")
    assert token, "the context processor should create a CSRF token"
    return token
