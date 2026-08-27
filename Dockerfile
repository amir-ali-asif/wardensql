FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

COPY shop.sqlite ./shop.sqlite

# Run as a non-root user.
RUN useradd -m appuser
USER appuser

EXPOSE 8000

# Multiple workers for concurrency; tune to CPU count in production.
CMD ["sh", "-c", "uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
