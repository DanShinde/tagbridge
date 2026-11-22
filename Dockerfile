# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[all]"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="tagbridge" \
      org.opencontainers.image.description="Modbus and OPC UA to MQTT gateway with Sparkplug B" \
      org.opencontainers.image.source="https://github.com/DanShinde/tagbridge" \
      org.opencontainers.image.licenses="MIT"
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels "tagbridge[all]" \
 && useradd --system --uid 10001 --no-create-home tagbridge \
 && mkdir /config && chown tagbridge /config
ENV PYTHONUNBUFFERED=1
USER 10001
WORKDIR /config
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1
ENTRYPOINT ["tagbridge"]
CMD ["run", "/config/tagbridge.yaml"]
