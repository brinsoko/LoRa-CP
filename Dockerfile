# Dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install system deps if you ever need serial, etc.
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Ensure the instance dir exists inside the image
RUN mkdir -p /app/instance

ENV FLASK_ENV=production \
    PYTHONUNBUFFERED=1

EXPOSE 5000

# Gunicorn entrypoint (expects wsgi.py with app=create_app()).
# --timeout 120: the default 30s is too tight for Sheets-sync paths that
# can legitimately stall on the SheetsClient's rolling 60s quota throttle.
# Without this, a throttled call held the worker lock long enough that
# the unrelated /superadmin/sheets-status.json poll blocked, gunicorn
# SIGKILLed the worker, and the in-flight publish was lost.
# --workers 2: a single worker meant the entire app stalled behind a
# Sheets throttle (auth pages, judge UI, score writes — anything the
# worker was serving had to wait for the up-to-60s sleep). Two workers
# let unrelated requests proceed in parallel; small memory cost on a
# low-traffic admin tool.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "120", "--workers", "2", "wsgi:app"]