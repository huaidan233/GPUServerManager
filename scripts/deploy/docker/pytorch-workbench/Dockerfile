ARG BASE_IMAGE=gsm/python-workbench:py311-jlab4.6.4-20260930
FROM ${BASE_IMAGE}
COPY requirements.lock /opt/gsm/pytorch-requirements.lock
RUN --mount=type=bind,source=wheelhouse,target=/wheels,readonly \
    python -m pip install --no-index --no-deps --find-links=/wheels --require-hashes -r /opt/gsm/pytorch-requirements.lock \
    && python -m pip check \
    && python -m pip freeze > /opt/gsm/installed-requirements.txt
COPY check.py /usr/local/bin/gsm-pytorch-check
RUN chmod 0755 /usr/local/bin/gsm-pytorch-check \
    && python -c "import torch,torchvision,torchaudio; assert torch.__version__ == '2.11.0+cu128'; assert torch.version.cuda == '12.8'"
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility
LABEL gsm.environment="cuda" gsm.framework="pytorch" gsm.runtime.version="2.11.0" \
      gsm.cuda.version="12.8" gsm.applications="jupyter,tensorboard"
