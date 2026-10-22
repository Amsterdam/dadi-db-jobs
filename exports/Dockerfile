FROM python:3.11-slim-trixie AS builder

RUN apt update && apt install --no-install-recommends -y \
    build-essential \
    libgeos-dev \
    libpq-dev

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

# Start runtime image,
FROM python:3.11-slim-trixie
RUN useradd -U -r datadiensten -d /app

# Copy python build artifacts from builder image
COPY --from=builder /usr/bin/ /usr/bin/
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder /usr/local/lib/ /usr/local/lib/
COPY --from=builder /usr/local/lib/python3.11/site-packages/ /usr/local/lib/python3.11/site-packages/


# Does not work if installed in the builder step.
RUN apt update && apt install --no-install-recommends -y \
    libpq5 \
    gdal-bin \
    curl \
    zip \
    jq \
    netcat

WORKDIR /app
RUN mkdir tmp
RUN chown -R datadiensten: /app

COPY export.py ./
COPY slack.py ./
COPY update_azure_search.py ./
COPY split_files.sh ./
RUN chmod +x split_files.sh

USER datadiensten
CMD ["python",  "export.py"]
