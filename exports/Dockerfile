FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install --no-install-recommends -y \
    build-essential \
    libgeos-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

RUN apt-get update && apt-get install --no-install-recommends -y \
    libpq5 \
    gdal-bin \
    curl \
    zip \
    jq \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN useradd -U -r datadiensten -d /app && \
    mkdir tmp && \
    chown -R datadiensten: /app

COPY --from=builder --chown=datadiensten:datadiensten /app/.venv /app/.venv
COPY --chown=datadiensten:datadiensten split_files.sh ./
RUN chmod +x split_files.sh

ENV PATH="/app/.venv/bin:$PATH"

USER datadiensten
CMD ["python", "-m", "exports.export"]
