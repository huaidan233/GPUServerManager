ARG BASE_IMAGE=gsm/runtime-ssh:py311-base
FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime AS cuda-libraries
# These packages contain Python-version-independent CUDA shared libraries.
# Reuse the exact 2.5.1 dependency versions instead of downloading GBs twice.
RUN mkdir -p /opt/gsm/cuda-packages \
    && cp -a /opt/conda/lib/python3.11/site-packages/nvidia \
       /opt/conda/lib/python3.11/site-packages/nvidia_*.dist-info /opt/gsm/cuda-packages/

FROM ${BASE_IMAGE} AS science
COPY requirements-science.txt /opt/gsm/requirements-science.txt
RUN python -m pip install --no-cache-dir -r /opt/gsm/requirements-science.txt \
    && python -m pip install --no-cache-dir torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
    && python -m pip check \
    && python -m pip freeze > /opt/gsm/installed-requirements.txt
COPY environment-check.py /usr/local/bin/gsm-environment-check
RUN chmod 0755 /usr/local/bin/gsm-environment-check
LABEL gsm.environment="python" gsm.framework="pytorch-2.5.1" gsm.cuda="12.4"

FROM ${BASE_IMAGE} AS science312
COPY --from=cuda-libraries /opt/gsm/cuda-packages/ /usr/local/lib/python3.12/site-packages/
COPY requirements-science.txt /opt/gsm/requirements-science.txt
RUN python -m pip install --no-cache-dir -r /opt/gsm/requirements-science.txt \
    && python -m pip install --no-cache-dir torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
    && python -m pip check \
    && python -m pip freeze > /opt/gsm/installed-requirements.txt
COPY environment-check.py /usr/local/bin/gsm-environment-check
RUN chmod 0755 /usr/local/bin/gsm-environment-check
LABEL gsm.environment="python" gsm.framework="pytorch-2.5.1" gsm.cuda="12.4"

FROM ${BASE_IMAGE} AS golang
ARG GO_VERSION
ARG GO_SHA256
RUN test -n "${GO_VERSION}" && test -n "${GO_SHA256}" \
    && curl --fail --location --retry 3 "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tar.gz \
    && printf '%s  %s\n' "${GO_SHA256}" /tmp/go.tar.gz | sha256sum --check \
    && tar -C /usr/local -xzf /tmp/go.tar.gz && rm /tmp/go.tar.gz \
    && go version \
    && mkdir -p /home/researcher/go /home/researcher/.cache/go-build \
    && chown -R researcher:researcher /home/researcher/go /home/researcher/.cache
ENV GOPATH=/home/researcher/go GOTOOLCHAIN=local
LABEL gsm.environment="go" gsm.go.version="${GO_VERSION}"
