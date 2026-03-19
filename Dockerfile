FROM python:3.12-slim

# mDNS/Zeroconf needs avahi or we rely on host networking (simpler for home use)
# We use host networking in compose, so no special daemon needed here.

WORKDIR /app

RUN pip install --no-cache-dir pychromecast==14.0.4

COPY scraper.py .

VOLUME ["/data"]

ENV DB_PATH=/data/chromecast.db \
    POLL_INTERVAL=300 \
    DISCOVERY_TIMEOUT=15

CMD ["python", "-u", "scraper.py"]
