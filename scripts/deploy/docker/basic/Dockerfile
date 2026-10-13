ARG BASE_IMAGE=gsm/runtime-ssh:py312-base
FROM ${BASE_IMAGE}
ARG GSM_ENVIRONMENT
ARG GSM_RUNTIME_VERSION
LABEL gsm.environment="${GSM_ENVIRONMENT}" gsm.runtime.version="${GSM_RUNTIME_VERSION}"
COPY environment-check.py /usr/local/bin/gsm-basic-check
RUN chmod 0755 /usr/local/bin/gsm-basic-check \
    && rm -f /usr/local/bin/gsm-pytorch-cuda-check \
    && python -m pip uninstall -y ninja >/dev/null 2>&1 || true \
    && python -m pip check

# The CUDA base is intentionally assembled from the already verified CUDA
# runtime image. Install only the toolkit/compiler on top; this avoids a second
# unverified runtime and keeps the framework optional at the catalog level.
RUN if [ "${GSM_ENVIRONMENT}" = "cuda" ]; then \
      apt-get update && apt-get install -y --no-install-recommends wget gnupg \
      && wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb \
      && dpkg -i /tmp/cuda-keyring.deb && rm /tmp/cuda-keyring.deb \
      && apt-get update && apt-get install -y --no-install-recommends cuda-nvcc-12-4 \
      && rm -rf /var/lib/apt/lists/*; \
    fi
