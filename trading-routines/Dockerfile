FROM python:3.12-slim

# System deps: tzdata for APScheduler timezones, ca-certificates for HTTPS
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY trading_routines.py .
COPY config.yaml .
COPY prompts/ ./prompts/

# Reports dir mounted as volume
VOLUME ["/app/reports"]

# Non-root user for safety
RUN useradd --system --create-home --uid 1001 routines \
 && chown -R routines:routines /app
USER routines

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REPORTS_DIR=/app/reports \
    CONFIG_FILE=/app/config.yaml \
    PROMPTS_DIR=/app/prompts

# Default to running the scheduler; override with `docker compose run` for manual
CMD ["python", "trading_routines.py", "schedule"]
