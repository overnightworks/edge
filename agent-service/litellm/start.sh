#!/bin/sh
# Starts LiteLLM with the provider key from the file named by the first argument: exactly one
# non-empty printable line. A bad file ends the start with one line that names the path only.
set -eu
key_file=$1
if [ ! -f "$key_file" ] || [ "$(wc -l < "$key_file")" -gt 1 ] || ! grep -Eq '^[!-~]+$' "$key_file"; then
  echo "litellm: $key_file does not hold exactly one printable key line" >&2
  exit 1
fi
ANTHROPIC_API_KEY=$(cat "$key_file")
export ANTHROPIC_API_KEY
exec litellm --config /etc/litellm/config.yaml --host 0.0.0.0 --port 4000
