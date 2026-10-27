# The agent service image: every byte that enters it is pinned by digest or sha256.
# Built on the PC with the command in docs/agent-service.md; the context is this directory.

FROM docker.io/curlimages/curl:8.23.0@sha256:11e64e28c5f58c8d93ee95fcf22ac055f48b0bc440e0ce7d8013bbb5ad6aa8ac AS claude-download
ARG CLAUDE_VERSION=2.1.302
ARG CLAUDE_SHA256=60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5
RUN curl --fail --silent --show-error --location --output /tmp/claude \
        "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" \
    && echo "${CLAUDE_SHA256}  /tmp/claude" | sha256sum -c -

FROM docker.io/library/python:3.13.17-slim-trixie@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962
COPY --from=claude-download --chown=0:0 --chmod=0555 /tmp/claude /opt/claude/claude
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --only-binary :all: \
        -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt
USER 1000:1000
ENTRYPOINT ["agent-runtime-service"]
CMD ["--config", "/etc/agent-service/service.toml"]
