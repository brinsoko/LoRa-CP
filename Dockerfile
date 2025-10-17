# Dockerfile
FROM python:3.11-slim

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

# Gunicorn entrypoint (expects wsgi.py with app=create_app())
CMD ["gunicorn", "-b", "0.0.0.0:5000", "wsgi:app"]