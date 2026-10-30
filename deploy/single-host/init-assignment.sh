#!/bin/bash
# Initialize one published benchmark task. Attach Viv only after this exits successfully.
set -euo pipefail
task=${1:?Usage: init-assignment.sh TASK RUN_NAME}
run=${2:?Usage: init-assignment.sh TASK RUN_NAME}
TAC_ROOT=${TAC_ROOT:-/data/theagentcompany}
source_dir="$TAC_ROOT/source"
run_dir="$TAC_ROOT/runs/$run"
image="ghcr.io/theagentcompany/${task}-image:1.0.0"
container="tac-task-$run"
mkdir -m 700 "$run_dir"
printf '%s\n' "$task" > "$run_dir/task-name"
extra=()
if [ -n "${TAC_NPC_ENV_FILE:-}" ]; then extra+=(--env-file "$TAC_NPC_ENV_FILE"); fi
docker pull "$image"
docker create --name "$container" --label tac.environment=single-host --label "tac.assignment=$run" --network host \
  --add-host the-agent-company.com:192.0.2.10 -e SERVER_HOSTNAME=192.0.2.10 \
  "${extra[@]}" \
  -v "$source_dir/workspaces/tasks/$task/task.md:/instruction/task.md:ro" \
  -v "$source_dir/workspaces/base_image/init.sh:/utils/init.sh:ro" \
  -v "$source_dir/workspaces/base_image/reset.sh:/utils/reset.sh:ro" \
  "$image" tail -f /dev/null
docker start "$container"
docker exec "$container" bash /utils/init.sh
printf 'Initialized %s. Viv must work inside this container and read /instruction/task.md.\n' "$container"
printf 'Use docker exec / docker cp or the agent platform workspace adapter; do not expose /utils or credentials to the agent.\n'
