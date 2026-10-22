FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY the_tower ./the_tower
RUN pip install --no-cache-dir -e .
EXPOSE 8765
ENV TOWER_DB_PATH=/data/tower.sqlite
VOLUME ["/data"]
CMD ["python", "-m", "the_tower", "serve", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/tower.sqlite"]