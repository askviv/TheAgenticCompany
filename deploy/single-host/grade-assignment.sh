#!/bin/bash
# Snapshot a finished examinee workspace; judge in a separate trusted container.
set -euo pipefail
run=${1:?Usage: grade-assignment.sh RUN_NAME [TRAJECTORY_FILE]}
TAC_ROOT=${TAC_ROOT:-/data/theagentcompany}
source_dir="$TAC_ROOT/source"
run_dir="$TAC_ROOT/runs/$run"
task=$(cat "$run_dir/task-name")
image="ghcr.io/theagentcompany/${task}-image:1.0.0"
container="tac-task-$run"
auth_dir=${TAC_CODEX_HOME:-$HOME/.codex}
docker stop -t 30 "$container"
mkdir -p "$run_dir/workspace" "$run_dir/grader-utils"
docker cp "$container:/workspace/." "$run_dir/workspace/"
trusted_source="tac-grader-source-$run"
docker create --name "$trusted_source" "$image" >/dev/null
docker cp "$trusted_source:/utils/." "$run_dir/grader-utils/"
docker rm -v "$trusted_source" >/dev/null
for file in common.py config.py scoring.py eval.py encrypt.py; do
  cp "$source_dir/workspaces/base_image/$file" "$run_dir/grader-utils/$file"
done
cp "$source_dir/workspaces/tasks/$task/evaluator.py" "$run_dir/grader-utils/evaluator.py"
trajectory=()
if [ -n "${2:-}" ]; then
  cp "$2" "$run_dir/workspace/trajectory.txt"
  trajectory+=(--trajectory_path /workspace/trajectory.txt)
fi
# Preserve task-local grader imports and assets from the published image, while applying reviewed source fixes.
docker run --rm --user "$(id -u):$(id -g)" --workdir /tmp -e PYTHONPATH=/utils \
  -v "$run_dir/grader-utils:/utils" --entrypoint python_default "$image" -P /utils/encrypt.py
docker run --rm --name "tac-grader-$run" --label tac.environment=single-host --label "tac.assignment=$run" --user "$(id -u):$(id -g)" \
  --network host --add-host the-agent-company.com:192.0.2.10 \
  --workdir /tmp -e PYTHONPATH=/utils \
  -e CODEX_HOME=/codex-auth -e CODEX_JUDGE_MODEL=gpt-6-luna \
  -e DECRYPTION_KEY='theagentcompany is all you need' \
  -v "$auth_dir:/codex-auth" -v /usr/local/bin/codex:/usr/local/bin/codex:ro \
  -v "$run_dir/grader-utils:/utils" -v "$run_dir/workspace:/workspace" \
  --entrypoint python_default "$image" -P /utils/eval.py --result_path /workspace/result.json "${trajectory[@]}"
printf 'Result: %s/workspace/result.json\n' "$run_dir"
