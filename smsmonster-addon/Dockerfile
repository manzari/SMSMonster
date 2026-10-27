ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY app/ /app/
RUN pip install --no-cache-dir . 
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
