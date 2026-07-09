#!/bin/sh
set -e

echo "Waiting for PostgreSQL..."

while ! python -c "import socket; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(2); s.connect(('${DB_HOST:-db}', ${DB_PORT:-5432})); s.close()" 2>/dev/null; do
    sleep 1
done

echo "PostgreSQL is up - executing migrations..."

python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting Gunicorn..."
exec gunicorn KarenProject.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
