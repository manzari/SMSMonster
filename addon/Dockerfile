# Build context is the repository root: docker build -f addon/Dockerfile .
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}
ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY addon/run.sh /run.sh
RUN chmod a+x /run.sh
LABEL io.hass.name="SMS Monster" \
      io.hass.description="SMS gateway for SIM800C USB modems" \
      io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="${BUILD_ARCH}"
CMD ["/run.sh"]
