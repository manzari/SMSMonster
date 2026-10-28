FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SMSMONSTER_DATA_DIR=/data SMSMONSTER_PORT=8080
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status==200 else 1)"
CMD ["smsmonster"]

FROM runtime AS test
RUN pip install ".[test]"
COPY tests ./tests
RUN pytest -q
