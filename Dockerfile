# Reproducible offline runtime for the covenant agent.
#
# Two stages: dependencies are built with a toolchain that is not present in
# the final image, which then contains only the interpreter, the installed
# package, and Tesseract with Russian and English language data.

FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

# Tesseract plus the language data the documents actually need. Installing
# the language packs here means the image never has to reach the network at
# run time.
RUN apt-get update \
 && apt-get install --no-install-recommends -y \
      tesseract-ocr \
      tesseract-ocr-rus \
      tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user; the application tree stays read-only to it.
RUN useradd --create-home --uid 10001 agent

COPY --from=build /opt/venv /opt/venv
COPY --chown=root:root run.py /app/run.py
COPY --chown=root:root src /app/src

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    # Caches and outputs are written to mounted volumes, never into /app,
    # so the application tree can stay read-only.
    COVENANT_AGENT_DATASET_DIR=/data/input \
    COVENANT_AGENT_CACHE_DIR=/data/cache

WORKDIR /app
USER agent

ENTRYPOINT ["python", "/app/run.py"]
CMD ["--input", "/data/input", "--output", "/data/output/submission.json", \
     "--audit-dir", "/data/output/audit", "--report", "/data/output/run-report.json"]
