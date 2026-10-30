#!/bin/bash
# Stop only this benchmark. Preserve all containers, volumes, databases and images.
set -euo pipefail
# Release LAN port 443 before restoring Kubernetes.
sudo -n systemctl disable --now tac-https.service tac-https-certificate.timer
TAC_ROOT=${TAC_ROOT:-/data/theagentcompany}
source "$TAC_ROOT/source/deploy/single-host/host.env"
docker ps -q --filter label=tac.environment=single-host | xargs -r docker stop
docker compose --env-file "$TAC_ROOT/source/deploy/single-host/host.env" -p tac-controller \
  -f "$TAC_ROOT/source/deploy/single-host/controller.compose.yml" stop
docker compose --env-file "$TAC_ROOT/plane/plane-app/plane.env" -p plane-app \
  -f "$TAC_ROOT/plane/plane-app/docker-compose.yaml" stop
GITLAB_PORT=8929 docker compose -p theagentcompany \
  -f "$TAC_ROOT/source/servers/docker-compose.yml" \
  -f "$TAC_ROOT/source/deploy/single-host/apps.override.yml" stop
