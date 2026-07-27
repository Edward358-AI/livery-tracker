FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY livery_tracker/ livery_tracker/

# Runtime state (config, watchlist, flight events, airport cache) lives here;
# docker-compose mounts ./data so it survives container rebuilds.
ENV LT_DATA_DIR=/app/data
VOLUME ["/app/data"]

CMD ["python", "-m", "livery_tracker"]
