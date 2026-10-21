#!/bin/bash
# Go-live step 4a, run once inside the bootstrap keycloak container before the first apply: moves
# the master realm's front end to the admin entrypoint (KC_HOSTNAME_ADMIN), as keycloak/master.yaml
# declares. keycloak-config-cli cannot make this first change itself: Keycloak checks an admin
# token's issuer against the master realm's front end, so the change would invalidate
# config-cli's own token in the middle of its run.
set -euo pipefail

KC_CLI_PASSWORD="$(</run/secrets/KC_BOOTSTRAP_ADMIN_PASSWORD)"
export KC_CLI_PASSWORD
exec /opt/keycloak/bin/kcadm.sh update realms/master --no-config \
    --server http://localhost:8080 --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" \
    --set "attributes.frontendUrl=$KC_HOSTNAME_ADMIN"
