# Viv experiment library

Open the HTTPS dashboard at `https://experiments.company.example.test/experiments`. The private-LAN HTTP endpoint remains available for vetted author-file downloads. The React frontend uses shadcn/ui, Vite, and Tailwind. The library lists 105 non-coding workplace assignments and six financial company scenarios (A1–E1 and S01). Workplace cards retain reviewed copy from `briefs-*.json`; financial scenario details describe their objectives and evaluation criteria. Starting a financial scenario adds a case to the persistent shared company, so multiple cases can run against the same company records and actors. The company has one shared simulated clock. Workplace assignments still create independent Rocket.Chat threads, tracked across container restarts with webhook and server-sent-event updates.

These are normal requests to the existing Viv chat integration. The launcher does not implement a Viv responder or start/reset assignment containers. Runs share the current company services and data; concurrent tasks can change the same records/files. `Awaiting Viv` means delivery is confirmed; `Viv replied` means a message was received from that exact account, not that the assignment passed. Automated benchmark judging and isolated environment resets remain in the existing CLI tools and scoring is unchanged. A queued/sending run interrupted by restart becomes uncertain; retry reconciles its deterministic message ID before sending. Duplicate submissions of the same form reuse the same run.

The reviewed `servers/experiments/inputs.json` exposes only the 11 author-supplied files copied into agent-visible workspace/instruction paths by the non-coding task Dockerfiles. Requests needing these include direct download links. Evaluators and hidden reference assets are not exposed by this route. Existing ownCloud/company files remain in their normal services. Original benchmark hostnames in briefs are retained; Viv's existing tools/network must reach or map `the-agent-company.com` to the deployed applications. Current service links are in the deployment README.

## Deployment

The multi-stage Docker build compiles the React application with Node 24 and serves the resulting static assets from Flask; Node does not run on the host after the build. The coordinator client is packaged from `servers/experiments/coordinator.py` and reads only a dedicated controller token file; the coordinator's complete principals configuration is never mounted here.

The Run Results view is included in this same image. Building `servers/experiments` runs the web build stage and copies its output to `/opt/experiments/static/app`; there is no separate frontend container or mini-side Node process. For the Results API and score visibility contract, see [`docs/experiments-results.md`](../../docs/experiments-results.md).

Image: `tac-experiments:local`, container: `tac-experiments`, port 8110. The dashboard is intentionally available without an operator sign-in. Flask sessions retain CSRF tokens for state-changing browser requests; the internal coordinator API still uses its separate controller bearer token. The application follows the existing Flask runtime and uses one process with four dispatch workers, SQLite WAL, and threaded HTTP/SSE. It has no Docker socket or judge credentials. The Rocket.Chat event hook uses its independent webhook token.

Persistent data: `/data/theagentcompany/experiments-data/experiments.sqlite` (and SQLite WAL/SHM while running). For a consistent backup, stop this container then copy the directory. Configuration/secrets: `/data/theagentcompany/experiments.env`, mode 0600, deliberately outside Git. Required variables:

- `EXPERIMENTS_SECRET_KEY`: stable random Flask session secret.
- `EXPERIMENTS_WEBHOOK_TOKEN`: nonempty random outgoing webhook secret.
- `TAC_REPO=/repo`, `EXPERIMENTS_DB=/data/experiments.sqlite`.
- `ROCKETCHAT_BASE_URL=http://127.0.0.1:3000`.
- `ROCKETCHAT_PUBLIC_URL=https://chat.company.example.test`.
- `ROCKETCHAT_USERNAME`, `ROCKETCHAT_PASSWORD`: existing benchmark operator account with channel/integration permissions.
- `VIV_ROCKETCHAT_USERNAME=viv`.
- Optional `EXPERIMENTS_ROCKETCHAT_CHANNEL` (default `viv-experiments`), `EXPERIMENTS_PUBLIC_URL` (default `http://192.0.2.10:8110`).

Financial scenarios use these Compose settings (defaults shown):

- `SIMULATION_COORDINATOR_URL=http://192.0.2.10:8131`.
- The Compose file bind-mounts `/data/theagentcompany/enterprise/secrets/dashboard-controller-token` read-only at `/run/secrets/simulation-controller-token`.

Create the token file before starting this Compose service; the bind mount does not create a missing host path. It must contain only the coordinator's controller-only bearer token, be owned by host UID/GID 1000:1000, and have mode 0400 or 0600. Do not put token contents in `experiments.env`, `host.env`, code, or the dashboard browser. The coordinator URL may be overridden with `SIMULATION_COORDINATOR_URL` in the Compose environment.

On the host from `/data/theagentcompany/source`:

```sh
docker build -t tac-experiments:local servers/experiments
docker compose --env-file deploy/single-host/host.env -p tac-experiments -f deploy/single-host/experiments.compose.yml up -d --pull never
docker exec tac-experiments python -c 'from experiments.runner import RocketChatClient; print(RocketChatClient.from_env().setup_outgoing_webhook())'
```

To publish a Results UI or Experiments BFF change on the host, sync the reviewed source tree to `/data/theagentcompany/source`, then run these commands on the host. The Docker build compiles the React bundle and the Compose command recreates only the Experiments service; it preserves `/data/theagentcompany/experiments-data` and does not launch a company run or evaluation.

```sh
cd /data/theagentcompany/source
docker build -t tac-experiments:local servers/experiments
docker compose --env-file deploy/single-host/host.env -p tac-experiments -f deploy/single-host/experiments.compose.yml up -d --pull never --force-recreate experiments
docker compose --env-file deploy/single-host/host.env -p tac-experiments -f deploy/single-host/experiments.compose.yml ps experiments
curl --fail http://127.0.0.1:8110/api/health
```

The dashboard is available at `https://experiments.company.example.test/experiments` and the trusted LAN URL remains available for author-file downloads. Verify the sign-in screen and static bundle after the health check. Do not run an evaluation as a deployment smoke check: that creates a durable evaluation record. If the coordinator or its evaluation API changed, deploy its reviewed image separately before enabling the new proxy flow.

The idempotent integration setup creates `TACExperimentTracking` for `#viv-experiments`, event `sendMessage`, posting to `http://192.0.2.10:8110/api/chat-events`. It checks any existing integration matches rather than creating a second one. The secret is never returned. Changing the channel/URL/secret requires updating that integration as well.

Stop/start just the webpage without resetting company data:

```sh
docker compose --env-file deploy/single-host/host.env -p tac-experiments -f deploy/single-host/experiments.compose.yml stop
docker compose --env-file deploy/single-host/host.env -p tac-experiments -f deploy/single-host/experiments.compose.yml start
```

Before restoring Kubernetes, stop this Compose project too, then follow the preservation/restore instructions in README.md. No Kubernetes settings were changed by this addition.

## Verification — 29 October 2026

- Browser: deployed React/shadcn library search filtered 105 tasks to one result; full brief, Run experiment, message sync, durable run detail and tracker verified. A third request, `45196ee5-4236-40a6-800d-9036ab67f5c2` (spreadsheet questions), was submitted through the actual shadcn Run button. Earlier run records/messages survived the frontend migration and container recreation.
- Two actual requests delivered concurrently: `19e530ee-809f-4f5c-9b52-300eddb81635` (new graduate job description, with both source links) and `45d4262d-f951-4b47-982e-2d721420802f` (drinks survey). Distinct root message IDs confirmed.
- Native Rocket.Chat outgoing callbacks returned HTTP 200. A clearly labelled operator reply appeared automatically in the second run; it correctly remained `awaiting_viv` rather than being attributed to Viv.
- Live Rocket.Chat 5.3 missing-message behavior reproduced: HTTP 400 with exactly `{"success":false}`. The adapter handles this scoped native response; other failed lookups remain errors. Initial failed dispatch was retried under the same run ID and delivered once.
- All 17 focused Python tests pass, including URL-free display copy and unchanged dispatch instructions. React TypeScript/Vite build passes; lint has no errors (six warnings: generated component export conventions and asynchronous data-loading effect state). Integration tests exercise real Flask routes/SQLite state and replace only the external chat boundary: CSRF, duplicate submission, independent persistence, authenticated webhook/Viv reply, restart recovery. Adapter tests cover API failures and idempotent recovery.
- Neither live request had a Viv-authored reply at verification time. Viv execution/completion and its connector's handling of these tasks remain unverified. No result or score is inferred from delivery.

The original task-file checkout remains mounted at `/repo`; the company identity
JSON is separately bound read-only from the canonical enterprise source path.
This keeps the dashboard name/identity metadata aligned with the coordinator
without replacing the preserved task files. Recreate the dashboard container
after an atomic identity-file replacement so Docker binds the current file.
