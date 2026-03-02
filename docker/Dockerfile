FROM python:3.11-slim

LABEL org.opencontainers.image.title="aws-fargate-workload-runner"
LABEL org.opencontainers.image.description="Generic EDA stress/traffic generator for AWS Fargate"

WORKDIR /app

# Upgrade pip + setuptools before any installs (slim base ships with older versions)
RUN pip install --no-cache-dir --upgrade pip setuptools

# Install dependencies first (layer-cached separately from source)
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

# Install the awfr package
COPY src/ ./src/
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

# Non-root user for security
RUN addgroup --system awfr && adduser --system --ingroup awfr awfr
USER awfr

ENTRYPOINT ["awfr", "worker"]
