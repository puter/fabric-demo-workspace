# Polling / replay service image.
#   docker build -t mvoi-poller .
#   docker run --rm -e OHGO_API_KEY=... -e MVOI_PUBLISHER__KIND=stdout mvoi-poller
FROM python:3.11-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra fabric --no-editable

FROM python:3.11-slim
RUN useradd --create-home --uid 10001 mvoi
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config ./config
COPY reference-data ./reference-data
COPY replay/recordings ./replay/recordings
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MVOI_CONFIG_DIR=/app/config \
    MVOI_REFERENCE_DIR=/app/reference-data \
    MVOI_STATE__PATH=/state/mvoi_state.sqlite
USER mvoi
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
ENTRYPOINT ["mvoi"]
CMD ["run"]
