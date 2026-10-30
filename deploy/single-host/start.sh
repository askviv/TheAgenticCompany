#!/bin/bash
# Start existing initialized TAC state. This does not reset benchmark data.
set -euo pipefail
TAC_ROOT=${TAC_ROOT:-/data/theagentcompany}
GITLAB_PORT=8929 docker compose -p theagentcompany \
  -f "$TAC_ROOT/source/servers/docker-compose.yml" \
  -f "$TAC_ROOT/source/deploy/single-host/apps.override.yml" up -d --pull never
env -u COMPOSE_FILE docker compose --env-file "$TAC_ROOT/plane/plane-app/plane.env" -p plane-app \
  -f "$TAC_ROOT/plane/plane-app/docker-compose.yaml" up -d --pull never
docker compose --env-file "$TAC_ROOT/source/deploy/single-host/host.env" -p tac-controller \
  -f "$TAC_ROOT/source/deploy/single-host/controller.compose.yml" up -d --pull never

# HTTPS is a separate LAN-bound proxy; keep the preserved host edge unchanged.
sudo -n systemctl enable --now tac-https.service tac-https-certificate.timer
