FROM python:3.12.14-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

COPY requirements/constraints.txt /tmp/constraints.txt

RUN python -m venv "${VIRTUAL_ENV}" \
    && pip install -c /tmp/constraints.txt \
        "fastapi>=0.141.1" \
        "uvicorn>=0.30.0" \
        "pydantic>=2.7.0" \
        "httpx>=0.27.0" "websockets>=17.1" "packaging>=25" \
        "docker>=7.1.0"

FROM python:3.12.14-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY services/server-agent/gsm_server_agent /app/gsm_server_agent
COPY services/api/gsm_api/application_proxy.py /app/gsm_api/application_proxy.py

# The agent remains root because it manages the host Docker socket and storage roots.
EXPOSE 8000
CMD ["uvicorn", "gsm_server_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
