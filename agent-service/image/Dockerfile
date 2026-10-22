# The agent service image: every byte that enters it is pinned by digest or sha256.
# Built on the PC with the command in docs/agent-service.md; the context is this directory.

FROM docker.io/curlimages/curl:8.23.0@sha256:a134550be58ab783810e613ae96fd253c38f7bdd16d9bf9be350700c7a29ac06 AS claude-download
ARG CLAUDE_VERSION=2.1.302
ARG CLAUDE_SHA256=60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5
RUN curl --fail --silent --show-error --location --output /tmp/claude \
        "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" \
    && echo "${CLAUDE_SHA256}  /tmp/claude" | sha256sum -c -

FROM docker.io/library/python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
COPY --from=claude-download --chown=0:0 --chmod=0555 /tmp/claude /opt/claude/claude
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --only-binary :all: \
        -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt
USER 1000:1000
ENTRYPOINT ["agent-service"]
CMD ["--config", "/etc/agent-service/service.toml"]
