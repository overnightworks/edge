#!/bin/bash
# The container proof of `home setup`'s system step (#40, cells 1-13): a throwaway Ubuntu 24.04 with systemd as
# PID 1, never the live PC, never the real /etc or ~/.secrets. Cells 14-17 are unit tests (tests/test_next.py).
#
#   scripts/system-proof/run.sh <edge checkout> <evidence dir>
#
# <edge checkout> is a clone whose `origin/main` (or $EDGE_REF) carries tunnel/ and systemd/; only `git archive` reads it.
# It takes /tmp/probe-stack.lock itself (flock -o, so no child holds it), waits while the load is high, keeps the
# container ids in the evidence dir and removes both containers on exit. The sudo password is generated per run.
set -euo pipefail

if [[ -z ${SYSTEM_PROOF_LOCKED:-} ]]; then
    exec env SYSTEM_PROOF_LOCKED=1 flock -o /tmp/probe-stack.lock "$0" "$@"
fi

edge_checkout=${1:?the edge checkout to read tunnel/ and systemd/ from}
evidence=${2:?the directory for the evidence}
edge_ref=${EDGE_REF:-origin/main}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(git -C "$here" rev-parse --show-toplevel)
image=home-tools-system-proof
mkdir -p "$evidence"

until awk -v cores="$(nproc)" '{ exit !($1 <= 1.5 * cores) }' /proc/loadavg; do sleep 30; done

context=$(mktemp -d)
: >"$evidence/containers.txt"
cleanup() {  # the logs of every container are copied out first, so a failing proof keeps its evidence
    local container name
    while read -r container; do
        name=$(docker inspect --format '{{.Name}}' "$container" 2>/dev/null) || name=$container
        docker cp "$container:/proof/evidence" "$evidence/${name#/}" >/dev/null 2>&1 || true
        docker rm -f "$container" >/dev/null 2>&1 || true
    done <"$evidence/containers.txt"
    rm -rf "$context"
}
trap cleanup EXIT

mkdir -p "$context/context"
(cd "$repo" && timeout 300 uv build --wheel --out-dir "$context/context" >/dev/null)
git -C "$edge_checkout" archive "$edge_ref" tunnel systemd | tar -x -C "$context/context"
cp "$here/Dockerfile" "$context/"
cp "$here/drive.sh" "$here/terminal.exp" "$context/context/"
timeout 1800 docker build --quiet --tag "$image" "$context" >/dev/null

password=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')

wait_for_boot() {  # wait_for_boot <container>: until systemd reports running or degraded (units may have failed)
    # `is-system-running --wait` exits 1 at once while PID 1 has no bus yet, so it cannot be the wait: after a
    # restart it returned before the boot began, and the proof then read a system no unit had started in.
    local container=$1
    timeout 120 docker exec "$container" bash -c \
        'until [[ $(systemctl is-system-running 2>/dev/null) =~ ^(running|degraded)$ ]]; do sleep 1; done'
    return
}

start_container() {  # start_container <name>: systemd as PID 1, the tester's password set for this run only
    local name=$1 container
    container=$(docker run --detach --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
        --name "home-tools-system-proof-$name-$$" "$image")
    echo "$container" >>"$evidence/containers.txt"
    wait_for_boot "$container"
    printf 'tester:%s\n' "$password" | docker exec --interactive "$container" chpasswd
    echo "$container"
}

stage() {  # stage <container> <name>: one stage of drive.sh, its cell lines on stdout and in the evidence
    local container=$1 name=$2
    # The value reaches the container from docker's own environment, never from its arguments.
    PROOF_PASSWORD="$password" timeout 3600 docker exec --env PROOF_PASSWORD "$container" /proof/drive.sh "$name" \
        | tee -a "$evidence/cells.txt"
}

reboot() {
    local container=$1
    timeout 120 docker restart "$container" >/dev/null
    wait_for_boot "$container"
}

: >"$evidence/cells.txt"
fresh=$(start_container fresh)
stage "$fresh" install
stage "$fresh" fresh
reboot "$fresh"
stage "$fresh" after-marker-reboot
reboot "$fresh"
stage "$fresh" after-unmarked-reboot

prepared=$(start_container prepared)
stage "$prepared" install
stage "$prepared" prepared

judge() {  # judge <cells file>: every cell 1-13 printed a held line and none printed a failed one
    local cells=$1 cell missing=()
    for cell in {1..13}; do
        grep -q "^cell $cell: held: " "$cells" || missing+=("$cell")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "no held line for cell(s): ${missing[*]}" >&2
        return 1
    fi
    if grep -q ': FAILED: ' "$cells"; then
        grep ': FAILED: ' "$cells" >&2
        return 1
    fi
    return 0
}

judge "$evidence/cells.txt"
