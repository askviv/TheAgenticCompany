#!/usr/bin/env bash
# Package the native apps with Frappe's unmodified upstream image builder.
set -euo pipefail
hr_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
image="tac-erpnext-apps:2026-11-06"
if docker image inspect "$image" >/dev/null 2>&1; then
  exit 0
fi
builder_revision=46c7ced81efbd4a119e14b81bf78e3b77cad3309
builder_dir="$(mktemp -d)"
trap 'rm -rf -- "$builder_dir"' EXIT
git -C "$builder_dir" init --quiet
git -C "$builder_dir" remote add origin https://github.com/frappe/frappe_docker.git
git -C "$builder_dir" fetch --quiet --depth 1 origin "$builder_revision"
git -C "$builder_dir" checkout --quiet --detach FETCH_HEAD
docker build --platform linux/amd64 --tag "$image" \
  --file "$builder_dir/images/custom/Containerfile" \
  --build-arg FRAPPE_BRANCH=v16.54.0 \
  --secret "id=apps_json,src=$hr_dir/apps.json" "$builder_dir"
