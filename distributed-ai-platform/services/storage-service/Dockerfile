FROM python:3.11-slim-bookworm

WORKDIR /app

# Using pip to install the package dependencies from pyproject.toml
COPY services/storage-service/pyproject.toml .
RUN pip install --no-cache-dir .

# Copy shared libraries first (layer caching)
COPY shared/ /app/shared/

# Copy service code
COPY services/storage-service/ /app/

# Dataset files live in /data (a volume in docker-compose.yml). The service
# runs as an unprivileged user that owns only that directory.
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app /data
USER app

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
