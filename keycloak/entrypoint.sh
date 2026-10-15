#!/bin/bash
# Exports every file under /run/secrets as the environment variable it is named after, then
# replaces itself with the real command. Compose mounts each secret with its variable name as
# the target, so the value lives in the process environment only, never in the container's
# Config.Env, which `docker inspect` and Traefik's socket proxy can read.
set -euo pipefail

shopt -s nullglob
for secret_file in /run/secrets/*; do
    variable_name="$(basename "$secret_file")"
    if [[ ! "$variable_name" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
        echo "entrypoint: secret target '$variable_name' is not an environment variable name" >&2
        exit 1
    fi
    if [[ ! -s "$secret_file" ]]; then
        echo "entrypoint: secret file for $variable_name is empty" >&2
        exit 1
    fi
    export "$variable_name=$(<"$secret_file")"
done

exec "$@"
