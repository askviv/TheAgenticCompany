# Isolated actor workers

This deployment adds two private RPC workers to the host: a τ-bench customer
simulator and a CoffeeBench supplier. Both bind to loopback only and require
separate bearer credentials. The coordinator is configured with those URLs and
credentials by the bootstrap. The workers do not receive coordinator admin,
evaluator, Mailu, or ERPNext credentials.

## Runtime and upstream pins

the host uses its existing Python 3.12 and Codex CLI 0.165.0 login. The
bootstrap installs uv 0.9.24 from a PyPI wheel whose SHA-256 is pinned in
`uv-requirements.txt`, then synchronizes each committed lock with `--frozen`.
The worker services use separate venvs and state directories. Their Codex
transport uses the existing CLI login under `/home/company/.codex`, ignores
user configuration, disables native Codex tools, and returns structured data
to the upstream actor runtime.

| Worker | Upstream source | Reviewed revision | License | Locked environment |
| --- | --- | --- | --- | --- |
| Customer | [τ-bench](https://github.com/sierra-research/tau2-bench) | `7aad81c37a3a78b4ed3e4866769f851400c64415` | MIT | `servers/simulation/tau2_actor_runtime/uv.lock`, SHA-256 `e0821b68a3a26d20915520c6261d7303c236f389dee51d349a0aca16b0a2628d` |
| Supplier | [CoffeeBench](https://github.com/SakanaAI/CoffeeBench) | `9e2f3950b5812c8753de81ede9c68babc024b5c8` | Apache-2.0 | `third_party/coffeebench/upstream/uv.lock`, SHA-256 `5fb06c9075eeb73aeed35505ef2443033ee72f9d99a702719274cdac89736880` |

CoffeeBench's committed lock supplies the worker's RPC stack: FastAPI
0.123.10, Pydantic 2.12.5, and Uvicorn 0.40.0. The bootstrap uses `uv sync
--locked`, so it fails if either project metadata and its committed lock diverge
and does not resolve a different set of dependencies during deployment.

## Prepare private files and environments

Copy the reviewed source tree to `/data/theagentcompany/enterprise/source`,
then run:

```sh
sudo python3.12 /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/actors/bootstrap.py
```

Bootstrap tightens only the existing Codex home directory mode to `0700` after
checking that `company` owns it. It does not recurse through or rewrite the
authenticated CLI state. It creates per-worker SQLite state and private
mode-0600 environment files, installs the two frozen venvs, and updates only
`actor_workers` in the root-owned mode-0600 coordinator runtime configuration.
It does not install or start systemd units, make Codex requests, send mail, or
start a simulation. It never prints bearer values or CLI authentication data.

The coordinator request timeout is 600 seconds. Each Codex operation has a
180-second bound. The worker service caps are 2 GiB and 1.25 CPU for τ-bench,
and 2 GiB and 1 CPU for CoffeeBench. Together the actor memory caps are 4 GiB.
The limits keep actor processes bounded; they do not promise a particular
response time or inference availability.

## Review and enable the services

Review both unit files and the generated private config before installation.
The workers use `company`, `ProtectSystem=strict`, a read-only home except the
existing `.codex` directory needed by the CLI, loopback listeners on ports
8141/8142, private state directories, and no Docker socket. Logs contain
operator-safe diagnostics; actor content and bearer values do not belong in
journal output.

After review, install and enable explicitly:

```sh
sudo install -o root -g root -m 0644 \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/actors/tac-customer-actor.service \
  /etc/systemd/system/tac-customer-actor.service
sudo install -o root -g root -m 0644 \
  /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/actors/tac-supplier-actor.service \
  /etc/systemd/system/tac-supplier-actor.service
sudo systemctl daemon-reload
sudo systemctl enable --now tac-customer-actor.service tac-supplier-actor.service
```

Health-only checks do not trigger inference:

```sh
curl --fail http://127.0.0.1:8141/health
curl --fail http://127.0.0.1:8142/health
sudo systemctl status tac-customer-actor.service tac-supplier-actor.service
```

For rollback, stop and disable those two units. Keep their state directories and
private environment files for diagnosis or a later restart; do not remove or
reset them as part of rollback. The coordinator actor URLs will need to be
cleared by the operator if actor services are intentionally retired.
