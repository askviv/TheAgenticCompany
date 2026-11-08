#!/usr/bin/env bash
# Package commit-locked apps with native Git, BuildKit and Frappe Bench.
set -euo pipefail
hr_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
image=${ERP_IMAGE:-$(docker compose --file "$hr_dir/../compose.yml" --profile "*" config --format json |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["backend"]["image"])')}
builder=tac-business-apps
builder_revision=46c7ced81efbd4a119e14b81bf78e3b77cad3309
frappe_branch=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$hr_dir/framework.json")
manifest_digest=$(cat "$hr_dir/apps.json" "$hr_dir/framework.json" "$hr_dir/prepare-sources.py" | sha256sum | cut -d ' ' -f1)
if [[ ${ERP_IMAGE_OUTPUT:-type=docker} == type=docker ]] && docker image inspect "$image" >/dev/null 2>&1; then
  existing_manifest=$(docker image inspect --format '{{ index .Config.Labels "org.theagentcompany.apps-manifest.sha256" }}' "$image")
  existing_builder=$(docker image inspect --format '{{ index .Config.Labels "org.theagentcompany.frappe-builder.revision" }}' "$image")
  existing_frappe=$(docker image inspect --format '{{ index .Config.Labels "org.theagentcompany.frappe.branch" }}' "$image")
  if [[ $existing_manifest == "$manifest_digest" && $existing_builder == "$builder_revision" && $existing_frappe == "$frappe_branch" ]]; then
    exit 0
  fi
fi
builder_dir="$(mktemp -d)"
trap 'rm -rf -- "$builder_dir"' EXIT
git -C "$builder_dir" init --quiet
git -C "$builder_dir" remote add origin https://github.com/frappe/frappe_docker.git
git -C "$builder_dir" fetch --quiet --depth 1 origin "$builder_revision"
git -C "$builder_dir" checkout --quiet --detach FETCH_HEAD
python3 "$hr_dir/prepare-sources.py" "$builder_dir"
if ! docker buildx inspect "$builder" >/dev/null 2>&1; then
  docker buildx create --name "$builder" --driver docker-container >/dev/null
fi
docker buildx build --builder "$builder" --platform linux/amd64 --tag "$image" \
  --output "${ERP_IMAGE_OUTPUT:-type=docker}" \
  --label "org.theagentcompany.apps-manifest.sha256=$manifest_digest" \
  --label "org.theagentcompany.frappe-builder.revision=$builder_revision" \
  --label "org.theagentcompany.frappe.branch=$frappe_branch" \
  --build-arg CACHE_BUST="$manifest_digest" \
  --file "$builder_dir/images/custom/Containerfile" \
  --build-context "app_sources=$builder_dir/app-sources" \
  --build-arg FRAPPE_BRANCH=pinned \
  --build-arg FRAPPE_PATH=file:///opt/app-sources/frappe \
  --secret "id=apps_json,src=$builder_dir/app-sources/apps.json" "$builder_dir"
docker buildx stop "$builder"
