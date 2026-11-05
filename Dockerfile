# syntax=docker/dockerfile:1

# ybtop image: CLI, viewer and MCP server. Build with: docker build -t ybtop .
# EXTRAS picks the pip extras: mcp (default), mcp,histogram, or empty for none

FROM python:3.12-slim AS build
ARG EXTRAS=mcp
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[${EXTRAS}]"

FROM python:3.12-slim
LABEL org.opencontainers.image.title="ybtop" \
      org.opencontainers.image.description="Observability for YugabyteDB: statement stats, ASH, tablets, browser viewer and MCP server" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /install /usr/local

# Snapshots live in /data, run as non-root
RUN useradd --create-home --uid 10001 ybtop \
 && mkdir /data && chown ybtop:ybtop /data
USER ybtop
WORKDIR /data
VOLUME /data

# 8765: viewer, 8766: MCP (with --mcp)
EXPOSE 8765 8766
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["ybtop"]
CMD ["--help"]
