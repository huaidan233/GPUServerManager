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
        "sqlalchemy>=2.0.0" \
        "httpx>=0.27.0" \
        "websockets>=17.1" \
        "python-dotenv>=1.2.3" \
        "cryptography>=50.0.1" \
        "pypdf>=6.19.0" \
        "Pillow>=11.0.0" \
        "python-multipart>=0.0.32" \
        "argon2-cffi>=23.1.0" \
        "pyotp>=2.9.0" \
        "tzdata>=2026.3"

FROM python:3.12.14-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 gsm \
    && useradd --system --uid 10001 --gid gsm --home-dir /nonexistent --shell /usr/sbin/nologin gsm

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY services/api/gsm_api /app/gsm_api
COPY services/print-agent /app/deploy-bundle/services/print-agent
COPY scripts/deploy/install_print_agent_windows.ps1 scripts/deploy/uninstall_print_agent_windows.ps1 /app/deploy-bundle/scripts/deploy/
ENV GSM_DEPLOY_BUNDLE_ROOT=/app/deploy-bundle

RUN printf '#!/bin/sh\nexec python -m gsm_api.backup_cli "$@"\n' > /usr/local/bin/gsm-backup \
    && printf '#!/bin/sh\nexec python -m gsm_api.print_cli "$@"\n' > /usr/local/bin/gsm-print-agent-admin \
    && printf '#!/bin/sh\nexec python -m gsm_api.member_cli "$@"\n' > /usr/local/bin/gsm-member-admin \
    && chmod 0755 /usr/local/bin/gsm-backup /usr/local/bin/gsm-print-agent-admin /usr/local/bin/gsm-member-admin

USER gsm

EXPOSE 8000
CMD ["uvicorn", "gsm_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
