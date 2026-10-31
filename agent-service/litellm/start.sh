#!/bin/sh
# Starts LiteLLM with the provider key from the file named by the first argument and the master
# key from the second: each exactly one non-empty printable line. A bad file ends the start with
# one line that names the path only; a key never reaches argv or a log.
set -eu
read_key() {
  key_file=$1
  if [ ! -f "$key_file" ] || [ "$(grep -c '' "$key_file")" -ne 1 ] || LC_ALL=C grep -Evq '^[!-~]+$' "$key_file"; then
    echo "litellm: $key_file does not hold exactly one printable key line" >&2
    exit 1
  fi
  cat "$key_file"
}
ANTHROPIC_API_KEY=$(read_key "$1")
LITELLM_MASTER_KEY=$(read_key "$2")
export ANTHROPIC_API_KEY LITELLM_MASTER_KEY
exec litellm --config /etc/litellm/config.yaml --host 0.0.0.0 --port 4000
