ARG BASE_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
FROM ${BASE_IMAGE}

LABEL gsm.ssh.authorized_keys="disabled"
LABEL gsm.ssh.uid="10000" gsm.ssh.gid="10000"

ARG GSM_SSH_USER=researcher
ARG GSM_EXTRA_PACKAGES=
ENV GSM_SSH_USER=${GSM_SSH_USER}

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server git curl ca-certificates \
       build-essential pkg-config cmake ninja-build rsync unzip less nano tmux libgomp1 libgl1 libglib2.0-0 ${GSM_EXTRA_PACKAGES} \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10000 "${GSM_SSH_USER}" \
    && useradd --uid 10000 --gid 10000 --create-home --shell /bin/bash "${GSM_SSH_USER}" \
    && rm -f /etc/ssh/ssh_host_* \
    && mkdir -p /run/sshd /workspace /datasets /models /scratch \
    && chown -R "${GSM_SSH_USER}:${GSM_SSH_USER}" /workspace /datasets /models /scratch \
    && sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config \
    && echo "AllowUsers ${GSM_SSH_USER}" >> /etc/ssh/sshd_config \
    && install -m 0644 /dev/null /etc/ssh/gsm_user_ca.pub \
    && install -m 0644 /dev/null /etc/ssh/gsm_authorized_principals \
    && printf '%s\n' \
      'TrustedUserCAKeys /etc/ssh/gsm_user_ca.pub' \
      'AuthorizedPrincipalsFile /etc/ssh/gsm_authorized_principals' \
      'AuthorizedKeysFile none' \
      >> /etc/ssh/sshd_config \
    && printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -euo pipefail' \
      'python - <<'"'"'PY'"'"'' \
      'import torch' \
      'print(f"torch={torch.__version__}")' \
      'print(f"torch.cuda.is_available()={torch.cuda.is_available()}")' \
      'if not torch.cuda.is_available():' \
      '    raise SystemExit("CUDA is not available to PyTorch")' \
      'if torch.cuda.is_available():' \
      '    print(f"cuda_device_count={torch.cuda.device_count()}")' \
      '    print(f"cuda_device_name={torch.cuda.get_device_name(0)}")' \
      'PY' \
      > /usr/local/bin/gsm-pytorch-cuda-check \
    && chmod 0755 /usr/local/bin/gsm-pytorch-cuda-check \
    && printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -euo pipefail' \
      'ssh-keygen -A >/dev/null' \
      'ssh_user="${GSM_SSH_USER:-researcher}"' \
      'ssh_home="$(getent passwd "${ssh_user}" | cut -d: -f6)"' \
      'if [[ -n "${GSM_SSH_TRUSTED_CA_PUBLIC_KEY:-}" && -n "${GSM_SSH_OWNER_PRINCIPAL:-}" ]]; then' \
      '  printf "%s\n" "${GSM_SSH_TRUSTED_CA_PUBLIC_KEY}" > /etc/ssh/gsm_user_ca.pub' \
      '  printf "%s\n" "${GSM_SSH_OWNER_PRINCIPAL}" > /etc/ssh/gsm_authorized_principals' \
      '  chmod 0644 /etc/ssh/gsm_user_ca.pub /etc/ssh/gsm_authorized_principals' \
      'fi' \
      'if [[ -n "${GSM_SSH_SMOKE_AUTHORIZED_KEY:-}" ]]; then' \
      '  install -d -m 0700 -o "${ssh_user}" -g "${ssh_user}" "${ssh_home}/.ssh"' \
      '  printf "%s\n" "${GSM_SSH_SMOKE_AUTHORIZED_KEY}" > "${ssh_home}/.ssh/authorized_keys"' \
      '  chown "${ssh_user}:${ssh_user}" "${ssh_home}/.ssh/authorized_keys"' \
      '  chmod 0600 "${ssh_home}/.ssh/authorized_keys"' \
      'fi' \
      'exec "$@"' \
      > /usr/local/bin/gsm-ssh-entrypoint \
    && chmod 0755 /usr/local/bin/gsm-ssh-entrypoint

# sshd resets PATH for remote commands; make the selected runtime available in
# SSH commands, login shells, VS Code and the web terminal alike.
ENV PATH=/home/researcher/.local/bin:/usr/local/cuda/bin:/usr/local/go/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN printf '%s\n' "PATH=${PATH}" 'LANG=C.UTF-8' > /etc/environment \
    && printf '%s\n' "export PATH=${PATH}" > /etc/profile.d/gsm-runtime.sh \
    && printf '%s\n' ". /etc/profile.d/gsm-runtime.sh" >> /home/researcher/.bashrc

WORKDIR /workspace
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/gsm-ssh-entrypoint"]
CMD ["/usr/sbin/sshd", "-D"]
