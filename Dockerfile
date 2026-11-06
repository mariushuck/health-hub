FROM python:3.13-slim

RUN pip install --no-cache-dir uv==0.13.6

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY *.py ./
COPY providers ./providers

ENV PYTHONUNBUFFERED=1
# Standardnutzer des Pi: Dateien in data/<person>/ bleiben für Backups lesbar.
USER 1000:1000
CMD ["/app/.venv/bin/python", "sync.py", "--daemon"]
