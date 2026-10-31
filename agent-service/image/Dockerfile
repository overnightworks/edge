# The agent service image: every byte that enters it is pinned by digest or sha256.
# Built on the PC with the command in docs/agent-service.md; the context is this directory.

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS claude-download
ARG CLAUDE_VERSION=2.1.302
ARG CLAUDE_SHA256=60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5
# BuildKit checks the file's sha256 while it fetches it, but serves a file it already holds by that
# digest without fetching; the version check refuses a bumped or absent version with a stale hash.
ADD --checksum=sha256:${CLAUDE_SHA256} --chmod=0555 \
    "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" \
    /tmp/claude
RUN expected="${CLAUDE_VERSION} (Claude Code)" \
    && reported="$(/tmp/claude --version)" \
    && if [ "${reported}" != "${expected}" ]; then \
        echo "claude --version reports '${reported}', not '${expected}'" >&2; exit 1; \
    fi

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS codex-download
ARG CODEX_VERSION=0.163.0
ARG CODEX_SHA256=beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4
# The archive's sha256 and the version are checked as for claude. The static musl executable is
# taken out of the archive by name.
ADD --checksum=sha256:${CODEX_SHA256} \
    "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
    /tmp/codex.tar.gz
RUN tar -xzf /tmp/codex.tar.gz -C /tmp codex-x86_64-unknown-linux-musl \
    && expected="codex-cli ${CODEX_VERSION}" \
    && reported="$(/tmp/codex-x86_64-unknown-linux-musl --version)" \
    && if [ "${reported}" != "${expected}" ]; then \
        echo "codex --version reports '${reported}', not '${expected}'" >&2; exit 1; \
    fi

FROM docker.io/library/python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999
COPY --from=claude-download --chown=0:0 --chmod=0555 /tmp/claude /opt/claude/claude
COPY --from=codex-download --chown=0:0 --chmod=0555 /tmp/codex-x86_64-unknown-linux-musl /opt/codex/codex
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --only-binary :all: \
        -r /tmp/requirements.txt \
    && pip check \
    && rm /tmp/requirements.txt
USER 1000:1000
ENTRYPOINT ["agent-runtime-service"]
CMD ["--config", "/etc/agent-service/service.toml"]
