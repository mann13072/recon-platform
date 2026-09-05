FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY packages ./packages
COPY apps/api ./apps/api
COPY workers ./workers
RUN pip install --no-cache-dir -e ".[dev]"

COPY . .

# Run as a non-root user (security baseline, spec section 53).
RUN useradd --create-home --uid 10001 recon && chown -R recon:recon /srv
USER recon

EXPOSE 8000
CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
