# Use official Python base image
FROM python:3.10-slim

# Set work directory
WORKDIR /app

# System deps (optional, minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirement spec first for layer caching
COPY requirements.txt ./

# Install Python deps
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0

# Expose port for Railway
EXPOSE 8080

# Start gunicorn (use shell so ${PORT} expands)
CMD ["sh", "-c", "gunicorn -w 2 -k gthread -b 0.0.0.0:${PORT:-8080} app:app"]

