ARG BASE_IMAGE=gsm/python-base:3.12.14-20260923
FROM ${BASE_IMAGE} AS base
ARG PYTHON_ARCHIVE
ARG PYTHON_SHA256
ARG PYTHON_VERSION
COPY ${PYTHON_ARCHIVE} /tmp/python.tar.gz
RUN echo "${PYTHON_SHA256}  /tmp/python.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/gsm-python \
    && tar -xzf /tmp/python.tar.gz --strip-components=1 -C /opt/gsm-python \
    && rm /tmp/python.tar.gz
ENV PATH=/home/researcher/.local/bin:/opt/gsm-python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN printf 'PATH=%s\nLANG=C.UTF-8\n' "$PATH" > /etc/environment \
    && printf 'export PATH=%s\n' "$PATH" > /etc/profile.d/gsm-runtime.sh \
    && python -c "import sys; assert sys.version.split()[0] == '${PYTHON_VERSION}'" \
    && python -m pip check
LABEL gsm.environment="python-base" gsm.runtime.version="${PYTHON_VERSION}" gsm.framework="python"

FROM base AS workbench
COPY wheelhouse /opt/gsm-wheelhouse
COPY requirements.in /opt/gsm/requirements-python.in
COPY workbench-check.py /usr/local/bin/gsm-workbench-check
RUN python -m pip install --no-index --find-links=/opt/gsm-wheelhouse -r /opt/gsm/requirements-python.in \
    && python -m pip check \
    && python -m pip freeze > /opt/gsm/installed-requirements.txt \
    && chmod 0755 /usr/local/bin/gsm-workbench-check \
    && rm -rf /opt/gsm-wheelhouse
LABEL gsm.environment="python" gsm.framework="python-workbench" gsm.applications="jupyter,tensorboard"
