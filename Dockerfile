ARG BUILD_FROM=python:3.12-slim
FROM $BUILD_FROM

# mDNS/Zeroconf needs avahi or we rely on host networking (simpler for home use)
# We use host networking in compose, so no special daemon needed here.

WORKDIR /app

RUN pip install --no-cache-dir pychromecast==14.0.4

COPY run.sh /
RUN chmod a+x /run.sh

COPY scraper.py .

VOLUME ["/data"]

ENV DB_PATH=/data/chromecast.db \
    POLL_INTERVAL=300 \
    DISCOVERY_TIMEOUT=15

CMD ["python", "-u", "scraper.py"]
