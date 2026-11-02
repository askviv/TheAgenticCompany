# Isolated actor workers

This deployment adds two private RPC workers: a τ-bench customer simulator and
a CoffeeBench supplier. Both bind to loopback only and require separate bearer
credentials. The coordinator is configured with those URLs and credentials by
the bootstrap. The workers do not receive coordinator admin, evaluator, Mailu,
or ERPNext credentials.

## Runtime and upstream pins

the host uses its existing Python 3.12 and Codex CLI 0.165.0 login. The
bootstrap installs uv 0.9.24 from a PyPI wheel whose SHA-256 is pinned in
`uv-requirements.txt`, then synchronizes each committed lock with `--frozen`.
The bootstrap defaults to the original local CLI configuration and requires
the existing account under `/home/company/.codex`. Remote inference is an
explicit option for the isolated guest. The guest sends only inference requests
over a private bearer-authenticated connection to the host's Codex CLI service.
The host service uses its existing login, ignores user configuration, disables
native Codex tools, and returns structured data. Actor prompts, tools,
histories, and business/evaluation policy remain in the worker.

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

By default, bootstrap checks that `company` owns the existing Codex home,
tightens only its directory mode to `0700`, and leaves the authenticated CLI
state untouched. For guest workers, provide the host endpoint and a separate
private model bearer-token file prepared by host setup:

```sh
sudo python3.12 /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/actors/bootstrap.py \
  --inference-url http://10.0.2.2:8143 \
  --inference-token-file /path/to/private/codex-inference-token
```

Remote mode does not require or copy the Codex account. It copies only the
bearer token into a company-owned mode-0600 guest file and configures both
workers to call the supplied endpoint. Both modes create per-worker SQLite
state and private environment files, install the frozen venvs, and update only
`actor_workers` in the root-owned mode-0600 coordinator configuration. Neither
mode installs or starts systemd units, makes Codex requests, sends mail, or
starts a simulation. Bootstrap never prints bearer values or CLI auth data.

Coordinator, inference, and provider HTTP requests have no transport timeout
or automatic replay. The worker service caps are 2 GiB and
1.25 CPU for τ-bench, and 2 GiB and 1 CPU for CoffeeBench. Together the actor
memory caps are 4 GiB.

## Review and enable the services

Install the Codex inference service on the Linux host after host setup supplies
its private `/home/company/.codex-inference.env` file. It runs from the
virtual-time-world `host-venv` and a minimal `company-source` package containing
only the Codex model and transport modules; it does not import the actor
workers. It uses `company`, `ProtectSystem=strict`, and read/write access only
to the existing `.codex` directory. It binds to host loopback port 8143,
reachable from the guest at `10.0.2.2:8143`.

Install the ordinary actor units for the local CLI configuration. Install the
`*-remote.service` units in the guest for remote inference; they have no Codex
home write access. Both variants use loopback ports 8141/8142 and private state
directories. Logs contain operator-safe diagnostics; actor content and bearer
values do not belong in journal output.

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

In the guest, install `tac-customer-actor-remote.service` and
`tac-supplier-actor-remote.service` instead of the local units. On the host,
install and enable `tac-codex-inference.service` after configuring its private
environment file. If the existing authenticated account differs from the unit's
service identity, configure a private systemd override on the host. Keep the
account-specific override outside the repository.

Health-only checks do not trigger inference:

```sh
curl --fail http://127.0.0.1:8141/health
curl --fail http://127.0.0.1:8142/health
sudo systemctl status tac-codex-inference.service tac-customer-actor.service tac-supplier-actor.service
```

For rollback, stop and disable the units installed on each system. Keep their state directories and
private environment files for diagnosis or a later restart; do not remove or
reset them as part of rollback. The coordinator actor URLs will need to be
cleared by the operator if actor services are intentionally retired.
