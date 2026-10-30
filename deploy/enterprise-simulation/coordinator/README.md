# Private coordinator host deployment

This directory contains the host bootstrap, operator client, and systemd unit
for the coordinator. These instructions prepare a separate enterprise source
checkout and private runtime data; they do not start the service automatically.
Review the generated configuration and artifacts before installing the unit.

## Source and host paths

Keep the established workplace checkout at `/data/theagentcompany/source`.
Place the reviewed enterprise simulation source in its own checkout or rsync
tree. If cloning from the current upstream worktree, use its existing
`main` branch:

```sh
sudo install -d -m 0700 /data/theagentcompany/enterprise
sudo git clone --branch main --single-branch <repository-url> \
  /data/theagentcompany/enterprise/source
git -C /data/theagentcompany/enterprise/source status --short
git -C /data/theagentcompany/enterprise/source branch --show-current
```

The separate source path is required so bootstrap never overwrites or edits
`/data/theagentcompany/source`. Runtime manifests record content hashes and, if
Git metadata exists, the revision and dirty state; rsync trees are identified
by their content hashes. Bootstrap writes only within `/data/theagentcompany/enterprise/` plus the new counterparty
credential file under `/data/theagentcompany/mailu/`. It does not replace or
edit `/data/theagentcompany/source` or employee credential records. It generates
a coordinator-local Compose file so ERP administrative containers also mount
the enterprise checkout rather than the existing workplace source.

The host already uses Python 3.12. The pinned runtime dependencies are installed
in `/data/theagentcompany/enterprise/venv`; the broad benchmark environment is
not installed on this host.

## Mailboxes and private credentials

Bootstrap uses the existing Mailu API and `mailu.env` token. It verifies that
`example.com` already exists and provisions only:

- `northwind-supplier@example.com`
- `cornerstone-customer@example.com`
- the five CoffeeBench counterparty mailboxes declared in the company profile
- the Airline, Telecom, and Bank support mailboxes declared in the company profile

The private runtime directory includes the existing four workflow mailboxes,
every company employee mailbox in `company-identities.json` whose existing
credential is present, and these trusted actor records:

| Actor ID | Kind | Mailbox |
| --- | --- | --- |
| `viv` | agent | `agent@agent.example.test` (existing smtp4dev capture route) |
| `requester` | employee | `sarahjohnson@example.com` |
| `warehouse` | warehouse | `alexturner@example.com` |
| `logistics` | logistics | `chenxinyi@example.com` |
| `manager` | approver | `liming@example.com` |
| `supplier` | supplier | `northwind-supplier@example.com` |
| `customer` | customer | `cornerstone-customer@example.com` |

It reads existing employee passwords from `employee-credentials.json` without
changing that file or any employee account. A missing credential affects only
that employee's portal/mailbox access; bootstrap reports the count and continues
for other configured employees. The four workflow mailboxes remain required.
The private `mail-credentials.json` also maps Viv's fixed `agent@agent.example.test`
identity to the dedicated Mailu `viv_reply_relay@example.com` SMTP login. That
mapping lets inbound mail be authenticated by Mailu's `smtp.auth` result while
keeping the visible From address unchanged. Provision the relay account with
the Mailu setup first; bootstrap fails clearly if its private credential is
missing or its non-spoofing SMTP-only metadata does not match.
The two new sandbox mailboxes use the explicitly requested `theagentcompany`
password and their credentials are stored in
`enterprise-counterparty-credentials.json`. Existing accounts without a
coordinator-managed password are never reset or adopted. An uncertain mailbox
create remains pending until the exact saved credential authenticates; a
concurrent account with a different password stops bootstrap for operator
reconciliation.

Run bootstrap only after source and private Mailu configuration are ready:

```sh
sudo python3.12 /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/bootstrap.py
```

The script creates private runtime configuration, an agent-only client file,
and the venv. It prints file locations and account outcomes, never passwords,
principal tokens, API bodies, or message content. It does not start the API or
install/enable the unit.

`secrets/principals.json` contains three generated credentials: controller,
evaluator, and agent. `secrets/agent-client.json` contains only the agent
credential and API URL. Do not copy the principals file, runtime config, state
database, or Mailu credentials into an agent's environment. The agent receives
only its own token through the reviewed agent harness configuration.

## Review and install the systemd unit

Before installation, review the generated `/data/theagentcompany/enterprise/config/runtime.json`,
`erpnext-compose.yml`, actor directory, and file ownership/modes. The unit runs
the trusted coordinator on the host as root because the existing ERP site
administrator invokes the host Docker CLI and creates mode-0600 site
credentials. The agent itself runs outside this process and receives no Docker
socket, site secret, controller/evaluator token, or evaluator database. Treat
access to port 8131 and the coordinator host as privileged. Uvicorn binds only
`192.0.2.10:8131`; configure the host firewall for intended clients.

After review, install and start explicitly:

```sh
sudo install -o root -g root -m 0644 \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/tac-enterprise-simulation.service \
  /etc/systemd/system/tac-enterprise-simulation.service
sudo systemctl daemon-reload
sudo systemctl enable --now tac-enterprise-simulation.service
sudo systemctl status tac-enterprise-simulation.service
```

Inspect safe service status with `journalctl -u tac-enterprise-simulation.service`.
The API's unauthenticated `/health` response reports only component status, not
private actor state or provider response bodies. Workers wait for native Mailu
IMAP IDLE notifications and smtp4dev's native SignalR `messageschanged` event
on `/hubs/notifications`; they never poll smtp4dev or advance simulated time.
The live Mini hub negotiation and client handshake were verified without
sending a test message. The event is mailbox-wide, so the handler wakes one
coalescing worker which pumps active runs and applies each run's exact
run/generation correlation. Clock advancement remains controller-only.

The employee-facing provider portal is a separate FastAPI service on port
8132. It authenticates staff against each existing Mailu mailbox and only shows
cases assigned to that managed employee identity. Its employee interface has no
task criteria or evaluator endpoints. Bootstrap writes its private runtime
configuration and persistent session/integration keys but does not start it.
After reviewing the configured portal URL and provider Mailu identities, install
and start it explicitly:

```sh
sudo install -o root -g root -m 0644 \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/tac-company-provider-portal.service \
  /etc/systemd/system/tac-company-provider-portal.service
sudo systemctl daemon-reload
sudo systemctl enable --now tac-company-provider-portal.service
sudo systemctl status tac-company-provider-portal.service
```

It shares the coordinator's durable SQLite state database and sends all provider
requests and replies through the configured Mailu mailboxes. The internal case
registration key is stored separately from the operator API token and is never
sent to a browser.

The customer and supplier actors run as separate `company` systemd services:
`tac-customer-actor.service` on loopback port 8141 and
`tac-supplier-actor.service` on loopback port 8142. Their bearer tokens are
stored in the private coordinator config and mode-0600 worker environment
files. Each has its own lockfile-backed venv and SQLite state directory. The
customer uses the pinned τ-bench runtime; the supplier uses the pinned
CoffeeBench harness. See the [actor deployment guide](../actors/README.md) for
bootstrap, service management, and the frozen dependency versions. Neither
worker exposes its port beyond loopback. The Codex adapter does not resubmit a
whole actor turn after an uncertain result; this describes the adapter's
behavior, not retry behavior inside the underlying CLI/provider protocol.

The source project's [SignalR testing guide](https://github.com/rnwood/smtp4dev/blob/master/docs/Testing.md)
documents the hub endpoint and `messageschanged` callback. The coordinator uses
the pinned Python [SignalR Core client](https://pypi.org/project/signalrcore/)
in `servers/simulation/requirements.txt`.

## Operator API client

Use the private `ops.py` requests client so bearer tokens stay in the mode-0600
principal file and never appear in shell history or process arguments. Use
private payload files (`umask 077`) for request data. Every mutation requires a
stable `--key`; retry the same request with the same key.

```sh
sudo /data/theagentcompany/enterprise/venv/bin/python \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/ops.py health
```

Start an example C1 run (the payload is not secret, but keep request files
private if they contain business data):

```sh
umask 077
cat > /tmp/c1-start.json <<'JSON'
{"scenario_id":"C1","seed":17,"agent_id":"viv","agent_configuration":{}}
JSON
sudo /data/theagentcompany/enterprise/venv/bin/python \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/ops.py \
  start --payload /tmp/c1-start.json --key c1-run-17
```

The start response includes the new `run_id`. Advance time with a private JSON
payload file, replacing the placeholder with that returned UUID:

```json
{"run_id":"<run-uuid>","until":"2026-11-03T09:00:00+00:00"}
```

```sh
sudo /data/theagentcompany/enterprise/venv/bin/python \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/ops.py advance \
  --payload /tmp/advance-day-2.json --key c1-day-2
```

The client also provides `schedule-event` and `pending-events` for the
controller's canonical event queue, plus `pause`, `reset`, `evaluate`, `case`,
`inbox`, `erp-read`, `review`, `action`, `message`, and `pump`. `schedule-event`
posts a validated canonical event with a stable idempotency key. `pending-events`
reads the private, generation-scoped pending queue. Both commands require the
controller token; the agent client cannot inspect or inject canonical events.
The client selects a role-specific token internally. The `action` command allows the native
`create_goods_receipt` and `commit_expedited_shipping` operations. Both require
an authenticated coordinator grant and an idempotency key. The shipping action
creates a separate native service Purchase Order for the authorized supplier
quote; it does not change the component Purchase Order. Agent reads and actions
remain scoped to the agent's own run. Finance grants are generated by the
simulated manager policy; the operator client does not issue them. Evaluator
output is private and must not be given to the agent.

An uncertain SMTP result is shown as `mail_delivery: uncertain` in `/health`
and as an `uncertain` item in the operator `pump` response. Reconciliation
checks the exact saved Message-ID and run headers before marking delivery
complete; it never resubmits an already-started SMTP operation.

An agent harness should read `agent-client.json` directly from a controlled
secret mount, then send its token to the API using an Authorization header.
Never place the token in a URL, command-line argument, application log, or
trajectory.

## Optional operator action: scripted S01 reference

Run the reference driver only as an explicitly requested operator action. It is
not part of default design/integration acceptance and does not measure Viv or
LLM performance. It exercises the coordinator against its configured native
Mailu and ERPNext providers; its report labels actor mode, execution and model
cost accordingly. Stop the API service first so its mailbox workers cannot
compete with the direct reference process. Keep ERPNext, Mailu and smtp4dev
running. The service remains stopped when this command exits.

```sh
sudo systemctl stop tac-enterprise-simulation.service
sudo systemctl is-active tac-enterprise-simulation.service
```

Run the script as root with the deployed venv and source tree. `build_app`
returns `(app, config)`, and the app lifespan starts the native mailbox workers
and closes provider sessions on exit. `asyncio.to_thread` keeps the synchronous
engine driver from blocking the lifespan event loop. The reference writes its
private report with mode `0600` in a `0700` directory.

```sh
sudo env PYTHONPATH=/data/theagentcompany/enterprise/source \
  /data/theagentcompany/enterprise/venv/bin/python - <<'PY'
import asyncio
from pathlib import Path

from servers.simulation.reference import run_s01_reference
from servers.simulation.runtime import build_app

async def main():
    app, _config = build_app(
        "/data/theagentcompany/enterprise/config/runtime.json"
    )
    async with app.router.lifespan_context(app):
        result = await asyncio.to_thread(
            run_s01_reference,
            app.state.engine,
            start_key="native-s01-example-17",
            controller_actor_id="simulation-controller",
            seed=17,
            report_path=Path(
                "/data/theagentcompany/enterprise/reports/s01-example-native-17.json"
            ),
        )
        print({
            "run_id": result["run_id"],
            "trajectory": result["trajectory"],
            "checks": result["evaluation"]["checks"],
            "environment_drift": result["evaluation"].get("environment_drift"),
        })

asyncio.run(main())
PY
```

Use a new, reviewed `start_key` for each clean reference run; the run UUID is
deterministic from controller identity and key. A failed reference is a
preserved diagnostic, not a signal to blindly restart the whole script. The
driver has stable per-operation idempotency keys and provider reconciliation,
but a partial trajectory may already have completed external email or native
financial operations. Inspect the run, outbox, audit and native ERP state before
any continuation. Reconcile an uncertain outbox with the operator `pump` action
using its existing run; never resend a message with a new key. Use `reset` only
as a reviewed controller action after confirming the intended native restore
and audit consequences. Do not resume earlier diagnostic runs as a clean proof.

## Stop, resume and restore Kubernetes

To stop the three enterprise systemd services and ERPNext containers while
preserving all sites, backups, secrets and audit data:

```sh
sudo systemctl disable --now tac-company-issues.timer
sudo systemctl stop 'tac-issue-handoff-*.timer' 'tac-issue-handoff-*.service' tac-company-issues.service
sudo systemctl disable --now tac-enterprise-simulation.service tac-customer-actor.service tac-supplier-actor.service
sudo docker compose -f /data/theagentcompany/enterprise/config/erpnext-compose.yml stop
```

Do not use `down --volumes`, uninstall MariaDB or remove the enterprise runtime
directories. To resume the same state:

```sh
sudo docker compose -f /data/theagentcompany/enterprise/config/erpnext-compose.yml up -d
sudo systemctl enable --now tac-customer-actor.service tac-supplier-actor.service tac-enterprise-simulation.service tac-company-issues.timer
```

Before restoring the host's preserved Kubernetes environment, stop these added
enterprise services, then follow the existing workplace stop and Kubernetes
restore procedure in [the host deployment guide](../../single-host/README.md).
The preserved `k3s.service.original`, control-plane datastore, workload data and
persistent volumes remain the restore source. The enterprise deployment does
not unmask k3s or change Kubernetes replicas.

The Mac's existing smtp4dev Kubernetes service is separate from the stopped
mini k3s environment. The enterprise coordinator neither recreates it nor
removes its database. Its existing launchd port forwards continue to serve the
Viv inbox after the coordinator stops.


## Company disruption notifications and employee handoff

E-CommerceBench logistics conditions are linked only to CoffeeBench orders whose
native source envelope explicitly records that condition at acceptance. The
adapter creates one Issue per source event and responsible employee in the
existing company. It retains the immutable source snapshot and original order
commitments; it does not guess changed quantities, prices or delivery dates.
An order merely falling inside a disruption window is insufficient evidence.
Later attributable orders join the Issue through native Info comments with order links, without overwriting employee edits. The full source/order snapshot uses native field permission level 1; ordinary Issue readers use the linked orders and their existing permissions.

`deploy/single-host/company-identities.json` owns `erpRoles` and `company.issueRouting`.
The logistics route currently resolves Chen (Delivery User). Native DocShare gives
the employee read/write access to the Issue and read access to its affected
orders, without broadening access to unrelated orders. ERPNext's Custom-event
Notification **Company Supplier Disruption** creates its native Communication
and Email Queue in the Issue transaction. Only that owned queue is sent; the
global ERP scheduler and unrelated queued email remain unchanged.

The coordinator checks only these tracked Message-IDs using read-only IMAP
metadata. The deadline is **five real-world minutes from Mailu's INTERNALDATE**,
independent of accelerated company time. An observed Seen flag is terminal.
At the deadline it checks again immediately before SMTP submission, then uses
the existing authenticated employee mailbox to send Viv “Please handle this,”
with the business description, requested outcome and actual ERPNext links.
This is a handoff rule, not simulated employee reasoning.

Private SQLite stores the receipt, deadline, immutable notification identity,
submission intent and final receipt. A compare-and-set claim and stable
Message-ID prevent competing/restarted workers from resending. If SMTP began
but its outcome is uncertain, the system reconciles that exact receipt; it never
blindly retries. `/health` reports unresolved failures for operator recovery.
IMAP cannot prove historical reads that were marked unread again entirely
between observations; the system preserves every read it actually observes.

Install the native recovery timer after deploying the reviewed source:

```sh
sudo install -m 0644 /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/coordinator/tac-company-issues.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tac-company-issues.timer
```

Each outstanding receipt gets a native systemd wall-clock timer (rounded up to
the next second, AccuracySec=1s). The recovery service reconciles source events
and durable receipts every minute, rebuilding transient timers after reboot.
Downtime can delay delivery, but cannot reset the stored deadline. These units
must be stopped alongside the coordinator as shown above before restoring k3s.

Integration evidence is in `../erpnext/admin/verification-results/`: native
source-to-order mapping, replay, exact scoped permissions and queue creation
were verified inside a rolled-back transaction; a separate labelled native
notification reached the responsible employee unread and its Seen flag was
verified. That fixture's ERP records were removed and its mailbox proof retained
read. Timeout, employee sender, restart/concurrency deduplication, pre-submit
reads and uncertain SMTP recovery use captured mail boundaries. No experiment
was run and no email was sent to Viv for these checks.

## Native benchmark feedback

The same recovery service now reconciles signed native ERP evidence, exact email
threads, and private benchmark action receipts. See the [feedback contracts,
coverage and verification guide](../../../docs/enterprise-simulation/company-feedback.md).
Controller/evaluator users can inspect `GET /company/feedback`; it includes private
financial evidence and is not an agent endpoint. No native benchmark APIs are
added to Viv's tools.

For a feature rollback, first stop the coordinator and recovery timer using the
commands above. In ERPNext's native Webhook list, disable only the 40 records
prefixed `TAC Company Evidence - ` before reverting coordinator code. Keep the
source snapshots, native financial records, SQLite ledgers and custom fields;
do not restore an older database over subsequent company work. Native checkpoints
and the actor SQLite stores must be backed up together. The pre-activation private
backup is `/data/theagentcompany/enterprise/backups/company-feedback-20261030T055711Z`.


## Recover a stale Coffee background operation

The controller-only `POST /company/background/coffeebench/recover` accepts
`{"previous_operation_id":"the exact pending operation ID"}` and an
`Idempotency-Key`. The existing operator CLI exposes it as `recover-coffee
--payload /private/path/recovery.json --key a-stable-recovery-key`. Reuse that key
when checking an uncertain response; a different key is a distinct recovery
attempt, not a retry. This is operational background recovery and can invoke
native actors. It must not be used as a deployment test.

Recovery verifies the worker-owned checkpoint and unchanged cursor under the
provider lock. It retains the old request/model-turn records, appends an audited
resolution, and uses a fresh operation ID with the original target and binding.
Exact completed inference responses are reused. Ambiguous pure actor inference
may be issued under the new audited attempt; this never retries judging. Native
Coffee actions and source results are committed atomically, and company email
uses its separate durable delivery receipts. A completed original operation is
replayed from its stored result instead of being executed again.

Older pending requests predate handoff receipts. Their E-Commerce proof is
derived only from actual completed worker operations, exact pinned event facts
and acknowledged finance intents. Derivation is recorded at the current time;
old requests are not rewritten or backdated. Missing/conflicting proof or a
changed/corrupt checkpoint remains an explicit integrity failure for operator
repair. Do not delete pending rows, reset the company, or clear inference caches
to bypass it.
