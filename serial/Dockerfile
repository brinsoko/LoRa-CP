FROM python:3.11-slim

WORKDIR /app
COPY serial/reader_client.py /app/reader_client.py
# requests only
RUN pip install --no-cache-dir requests

RUN apt-get update -qq && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

CMD ["python", "-u", "/app/reader_client.py"]