"""In-memory image validation and OCR failure-path tests."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from core import ocr


def png_bytes(width: int = 1200, height: int = 400, color: str = "white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_valid_png_is_read_without_touching_disk():
    image = ocr.validate_image(png_bytes(), "label.png")
    assert image.format == "PNG"
    assert image.size == (1200, 400)


def test_unsupported_extension_is_rejected_even_if_bytes_are_an_image():
    with pytest.raises(ocr.InvalidImage) as error:
        ocr.validate_image(png_bytes(), "label.pdf")
    assert str(error.value) == "unsupported_type"


def test_unreadable_data_is_rejected():
    with pytest.raises(ocr.InvalidImage):
        ocr.validate_image(b"not an image", "label.png")


def test_too_small_image_is_rejected():
    with pytest.raises(ocr.InvalidImage) as error:
        ocr.validate_image(png_bytes(20, 20), "label.png")
    assert str(error.value) == "too_small"


def test_size_limits_are_enforced_before_decoding():
    with pytest.raises(ocr.ImageTooLarge):
        ocr.validate_image(b"x" * (ocr.MAX_BYTES + 1), "label.png")


def test_pixel_limit_is_enforced_before_pixels_are_decoded(monkeypatch):
    class OversizedImage:
        format = "PNG"
        width = 10_000
        height = 10_000

        def load(self):
            raise AssertionError("load() must not run before the pixel check")

    monkeypatch.setattr(ocr.Image, "open", lambda *args, **kwargs: OversizedImage())
    with pytest.raises(ocr.ImageTooLarge) as error:
        ocr.validate_image(b"declared image header", "label.png")
    assert str(error.value) == "pixel_limit"


def test_decompression_bomb_error_is_reported_as_too_large(monkeypatch):
    def bomb(*args, **kwargs):
        raise ocr.Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(ocr.Image, "open", bomb)
    with pytest.raises(ocr.ImageTooLarge):
        ocr.validate_image(b"declared image header", "label.png")


def test_preprocess_converts_to_grayscale_and_upscales_small_images(monkeypatch):
    monkeypatch.setattr(ocr.pytesseract, "image_to_osd", lambda *a, **k: "Rotate: 0\n")
    image = ocr.validate_image(png_bytes(300, 100), "small.png")
    processed, warnings = ocr.preprocess(image)
    assert processed.mode == "L"
    assert processed.width == ocr.MIN_WIDTH
    assert warnings == []


def test_extract_text_reports_missing_tesseract(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: False)
    with pytest.raises(ocr.OCRUnavailable):
        ocr.extract_text(png_bytes(), "label.png")


def test_extract_text_returns_reviewable_text_and_confidence(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "available_languages", lambda: ("eng", "hin"))
    monkeypatch.setattr(ocr.pytesseract, "image_to_osd", lambda *a, **k: "Rotate: 0\n")
    monkeypatch.setattr(
        ocr.pytesseract,
        "image_to_string",
        lambda *a, **k: "Paracetamol 650 mg\n\n",
    )
    monkeypatch.setattr(
        ocr.pytesseract,
        "image_to_data",
        lambda *a, **k: {"conf": ["90", "80", "-1"]},
    )
    result = ocr.extract_text(png_bytes(), "label.png")
    assert result.text == "Paracetamol 650 mg"
    assert result.confidence == 85.0
    assert result.languages == ("eng", "hin")
    assert result.width == 1200


def test_empty_ocr_text_is_reported_as_a_warning(monkeypatch):
    monkeypatch.setattr(ocr, "ocr_available", lambda: True)
    monkeypatch.setattr(ocr, "available_languages", lambda: ("eng",))
    monkeypatch.setattr(ocr.pytesseract, "image_to_osd", lambda *a, **k: "Rotate: 0\n")
    monkeypatch.setattr(ocr.pytesseract, "image_to_string", lambda *a, **k: "\x0c \n")
    monkeypatch.setattr(ocr.pytesseract, "image_to_data", lambda *a, **k: {"conf": []})
    result = ocr.extract_text(png_bytes(), "label.png")
    assert result.text == ""
    assert "no_text_detected" in result.warnings
