FROM python:3.12-slim

# System deps for fpdf2 and general use
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application code
COPY . .

# Create directories for persistent data and uploads
RUN mkdir -p /data /app/uploads

# Non-root user for security
RUN useradd -r -s /bin/false appuser \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 5000

# Initialise DB then start gunicorn
CMD ["sh", "-c", "python -c 'from app import create_tables; create_tables()' && gunicorn -w 2 -b 0.0.0.0:5000 --timeout 120 --access-logfile - app:app"]
