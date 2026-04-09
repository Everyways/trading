# Multi-arch compatible (linux/amd64, linux/arm64 for Raspberry Pi)
FROM python:3.12-slim

# System deps — no C compilation required (pandas-ta chosen over ta-lib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Copy source
COPY . .

# Non-root user for security
RUN useradd -m -u 1000 bot && chown -R bot:bot /app
USER bot

EXPOSE 8000

CMD ["python", "-m", "app.main"]
