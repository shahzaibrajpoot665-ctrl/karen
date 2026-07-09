FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System dependencies for psycopg2 and image libraries
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        pkg-config \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        libpng-dev \
        libfreetype6-dev \
        liblcms2-dev \
        libwebp-dev \
        tcl-dev \
        tk-dev \
        libharfbuzz-dev \
        libfribidi-dev \
        libopenjp2-7-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --timeout 300 --retries 5 -r requirements.txt

# Copy project files
COPY . /app/

# Create directories for static and media
RUN mkdir -p /app/staticserve /app/media

# Collect static files
RUN python manage.py collectstatic --noinput || true

# Copy and set permissions for entrypoint
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
