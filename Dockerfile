FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY the_tower ./the_tower

RUN pip install --no-cache-dir . && \
    useradd --create-home --uid 10001 tower && \
    mkdir -p /data && chown tower /data
USER tower

EXPOSE 8765
ENV TOWER_DB_PATH=/data/tower.sqlite
VOLUME ["/data"]

ENTRYPOINT ["the-tower"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/tower.sqlite"]
