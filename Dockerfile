# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_ENV=production \
    PORT=8000

# Tesseract provides both English and Hindi OCR data. Images are processed in
# memory, so no upload directory is needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-hin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/instance \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=4)"

# SECRET_KEY must be supplied at run time. The image intentionally contains no
# secret and no database; SQLite is rebuilt from the bundled validated CSVs on
# startup. For a different data directory, mount a writable volume and set
# DATABASE_PATH.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --threads ${WEB_THREADS:-4} --timeout 60 --access-logfile - --error-logfile - wsgi:app"]
