"""In-memory image validation and OCR.

Design constraints:
* Uploaded images are never written to disk by application code.
* Prescription text is never logged.
* OCR output is returned for human review only; this module never interprets
  the text clinically.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

try:  # pytesseract is optional at import time so the site still runs without it
    import pytesseract
    from pytesseract import TesseractError, TesseractNotFoundError
except Exception:  # pragma: no cover - only hit in unusual installs
    pytesseract = None  # type: ignore[assignment]

    class TesseractError(Exception):
        pass

    class TesseractNotFoundError(Exception):
        pass


MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MIN_WIDTH = 1100
MAX_TEXT_CHARS = 5000


class OCRUnavailable(RuntimeError):
    """Tesseract is not installed or cannot be executed."""


class InvalidImage(ValueError):
    """The upload is not a supported, readable image."""


class ImageTooLarge(ValueError):
    """The upload exceeds the configured limits."""


@dataclass
class OCRResult:
    text: str
    confidence: float
    languages: tuple[str, ...]
    width: int
    height: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 1),
            "languages": list(self.languages),
            "width": self.width,
            "height": self.height,
            "warnings": list(self.warnings),
        }


def ocr_available() -> bool:
    if pytesseract is None:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def available_languages() -> tuple[str, ...]:
    if not ocr_available():
        return ()
    try:
        langs = tuple(sorted(pytesseract.get_languages(config="")))
    except Exception:
        return ("eng",)
    preferred = [lang for lang in ("eng", "hin") if lang in langs]
    return tuple(preferred or langs[:1] or ("eng",))


def validate_image(data: bytes, filename: str = "") -> Image.Image:
    if not data:
        raise InvalidImage("empty_file")
    if len(data) > MAX_BYTES:
        raise ImageTooLarge("too_large")
    lowered = (filename or "").lower()
    if "." in lowered:
        extension = "." + lowered.rsplit(".", 1)[-1]
        if extension not in ALLOWED_EXTENSIONS:
            raise InvalidImage("unsupported_type")
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge("pixel_limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImage("unreadable_image") from exc
    if image.format and image.format.upper() not in ALLOWED_FORMATS:
        raise InvalidImage("unsupported_type")
    # Check the declared dimensions before decoding pixels. Calling load() first
    # would let a small compressed "decompression bomb" allocate a huge buffer.
    if image.width * image.height > MAX_PIXELS:
        raise ImageTooLarge("pixel_limit")
    if image.width < 40 or image.height < 40:
        raise InvalidImage("too_small")
    try:
        image.load()
    except (OSError, ValueError) as exc:
        raise InvalidImage("unreadable_image") from exc
    return image


def preprocess(image: Image.Image, allow_osd: bool = True) -> tuple[Image.Image, list[str]]:
    warnings: list[str] = []
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, "white")
        converted = image.convert("RGBA")
        background.paste(converted, mask=converted.split()[-1])
        image = background
    else:
        image = image.convert("RGB")

    if image.width < MIN_WIDTH:
        ratio = MIN_WIDTH / float(image.width)
        image = image.resize(
            (MIN_WIDTH, max(1, int(image.height * ratio))), Image.Resampling.LANCZOS
        )

    gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=1)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    gray = gray.filter(ImageFilter.SHARPEN)

    if allow_osd:
        try:
            osd = pytesseract.image_to_osd(gray)
            rotation = 0
            for line in osd.splitlines():
                if line.lower().startswith("rotate:"):
                    rotation = int(line.split(":", 1)[1].strip())
            if rotation in (90, 180, 270):
                gray = gray.rotate(-rotation, expand=True)
                warnings.append(f"rotated_{rotation}")
        except Exception:
            # Orientation detection is best-effort; unreadable OSD is normal.
            pass
    return gray, warnings


def extract_text(
    data: bytes, filename: str = "", allow_osd: bool = True
) -> OCRResult:
    if not ocr_available():
        raise OCRUnavailable("tesseract_missing")
    image = validate_image(data, filename)
    width, height = image.width, image.height
    processed, warnings = preprocess(image, allow_osd=allow_osd)
    languages = available_languages() or ("eng",)
    config = "--oem 3 --psm 6 -c preserve_interword_spaces=1"
    try:
        text = pytesseract.image_to_string(processed, lang="+".join(languages), config=config)
    except TesseractNotFoundError as exc:
        raise OCRUnavailable("tesseract_missing") from exc
    except TesseractError as exc:
        raise InvalidImage("ocr_failed") from exc

    text = (text or "").replace("\x0c", "").strip()
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        warnings.append("text_truncated")

    confidence = 0.0
    try:
        data_map = pytesseract.image_to_data(
            processed, lang="+".join(languages), config=config, output_type=pytesseract.Output.DICT
        )
        values = []
        for raw in data_map.get("conf", []):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                values.append(value)
        if values:
            confidence = sum(values) / len(values)
    except Exception:
        confidence = 0.0

    if not text:
        warnings.append("no_text_detected")

    return OCRResult(
        text=text,
        confidence=confidence,
        languages=languages,
        width=width,
        height=height,
        warnings=tuple(warnings),
    )
