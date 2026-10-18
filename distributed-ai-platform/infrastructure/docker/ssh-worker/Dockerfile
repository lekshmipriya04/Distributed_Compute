# A throwaway test worker for `make up-dev` and the end-to-end tests: like a
# typical Linux PC (Debian, glibc, Python 3.12 with venv/pip), reachable over
# SSH on port 2222 as provider / providerpass. Never use it for real work.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server procps util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash provider \
    && echo 'provider:providerpass' | chpasswd \
    && mkdir -p /run/sshd \
    && rm -f /etc/ssh/ssh_host_* \
    && printf 'Port 2222\nPasswordAuthentication yes\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/worker.conf

EXPOSE 2222
# Each container gets its own host keys on first start (kept across restarts).
CMD ["sh", "-c", "ssh-keygen -A >/dev/null && exec /usr/sbin/sshd -D -e"]
