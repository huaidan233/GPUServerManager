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
        "httpx>=0.27.0" \
        "qrcode>=8.2"

FROM python:3.12.14-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 gsm \
    && useradd --system --uid 10001 --gid gsm --home-dir /nonexistent --shell /usr/sbin/nologin gsm

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY services/wechat-agent/gsm_wechat_agent /app/gsm_wechat_agent

USER gsm

EXPOSE 8000
CMD ["uvicorn", "gsm_wechat_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
