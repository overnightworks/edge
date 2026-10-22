# A throwaway Ubuntu 24.04 with systemd as PID 1, dbus and polkitd, for the container proof of `home setup`'s
# system step (#40). cloudflared comes from Cloudflare's apt repository; its config names a fake tunnel id, and
# the credential beside it is a dummy that only lets the unit try to start. No real secret is ever in here.
# python3 is the interpreter Ansible's modules run with, as on every PC; a bare ubuntu image has none.
FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dbus \
        expect \
        git \
        gnupg \
        polkitd \
        python3 \
        sudo \
        systemd \
        systemd-sysv \
    && mkdir -p --mode=0755 /usr/share/keyrings \
    && curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        --output /usr/share/keyrings/cloudflare-main.gpg https://pkg.cloudflare.com/cloudflare-main.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared noble main" \
        > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflared \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /uvx /usr/local/bin/

# `tester` is the operator: in the sudo group, its password set per run by run.sh. `bystander` is any other user.
ARG FAKE_TUNNEL_ID=00000000-0000-4000-8000-000000000000
RUN mkdir -p /etc/cloudflared \
    && printf 'tunnel: %s\ncredentials-file: /etc/cloudflared/%s.json\n' \
        "$FAKE_TUNNEL_ID" "$FAKE_TUNNEL_ID" > /etc/cloudflared/config.yml \
    && printf '{"AccountTag":"fake","TunnelSecret":"ZmFrZQ==","TunnelID":"%s"}\n' \
        "$FAKE_TUNNEL_ID" > "/etc/cloudflared/$FAKE_TUNNEL_ID.json" \
    && chmod 600 "/etc/cloudflared/$FAKE_TUNNEL_ID.json" \
    && useradd --create-home --shell /bin/bash --groups sudo tester \
    && useradd --create-home --shell /bin/bash bystander \
    && printf '%s\n' '[Unit]' 'Description=another unit for the polkit cells' \
        '[Service]' 'ExecStart=/bin/sleep infinity' \
        '[Install]' 'WantedBy=multi-user.target' \
        > /etc/systemd/system/proof-other.service \
    && systemctl enable proof-other.service

# The wheel of this checkout and edge's tunnel/ and systemd/ files, all staged by run.sh.
COPY context/ /proof/
RUN install -d -o tester -g tester /home/tester/git/edge \
    && cp -r /proof/tunnel /home/tester/git/edge/tunnel \
    && cp -r /proof/systemd /home/tester/git/edge/systemd \
    && chown -R tester:tester /home/tester/git \
    && chmod +x /proof/drive.sh /proof/terminal.exp

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
