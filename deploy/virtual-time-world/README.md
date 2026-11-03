# Isolated company VM

This provisions an Ubuntu 24.04 guest directly on the Linux host. Existing
company services and data directories are not mounted into it. Docker Compose
runs inside the guest, sharing its kernel clock.

Host packages:

```sh
sudo apt-get install --no-install-recommends qemu-system-x86 qemu-utils \
  cloud-image-utils libvirt-daemon-system libvirt-clients python3-libvirt virtinst
sudo bash deploy/virtual-time-world/provision.sh
```

The provisioner verifies the Ubuntu image checksum, preserves an existing
domain or disk, generates an SSH key on the host, and uses QEMU user networking
with SSH forwarded only on host localhost. It does not enable domain autostart
or change existing application addresses. It starts with one vCPU and 2 GiB
RAM while the original deployment remains running. Resource settings can be
provided through `WORLD_VCPUS`, `WORLD_MEMORY_MIB`, `WORLD_DISK_SIZE` and
`WORLD_SSH_PORT`. A separate guest-owned `company-data.qcow2` defaults to 250 GiB (`WORLD_DATA_DISK_SIZE`); cloud-init formats only a blank data disk, mounts its ext4 label at `/data`, and stores Docker and containerd data there. Docker uses native kernel port forwarding (`userland-proxy: false`) to avoid accelerating a separate proxy process startup deadline. Persistent artifacts live under `WORLD_ROOT` (default
`/data/theagentcompany/virtual-time-world`).

```sh
sudo virsh --connect qemu:///system domstate tac-virtual-time-world
sudo ssh -i /data/theagentcompany/virtual-time-world/config/ssh_ed25519 \
  -o UserKnownHostsFile=/data/theagentcompany/virtual-time-world/config/known_hosts \
  -p 22220 ubuntu@127.0.0.1
```

Cloud-init installs Docker, Compose and the guest agent. Guest NTP is disabled
for the later virtual-clock kernel. The original Ubuntu kernel remains in the
guest as a recovery path. The VM initially boots that stock kernel at normal
speed; creating it does not establish accelerated-clock functionality.

## Guest kernel

The candidate kernel is pinned to Linux 6.12.115. The source archive hash is
pinned in `kernel/build.sh`; `kernel/guest.config` retains the kernel features
needed for Docker. The builder installs the candidate alongside the stock
kernel and creates its initramfs, without changing the current boot selection.
Run it only inside the empty guest after installing the build dependencies.

## Guest Caddy and employee browser trust

The guest HTTPS front keeps the existing eight application hostnames and the
employee desktop's experiments hostname on the guest Caddy admin socket
`/run/tac-https/admin.sock`. The generic Caddyfile and systemd unit are in
`guest/`. At cutover, the host-side public Caddy remains the external TLS
endpoint; its employee desktop path forwards to guest port 443 through the
host-loopback QEMU forward on 18443. The guest Caddy uses the existing
long-valid company-clock leaf certificate and private key. The host's public
Let's Encrypt certificate remains the external certificate. Its leaf/key pair is also synchronized to the guest's externally published Mailu front; a separate internal Mailu front uses the long-valid company certificate on loopback and Docker's host gateway.

The cutover Caddyfile is staged privately at
`$WORLD_ROOT/config/guest-company-clock-Caddyfile` with mode 0600. It is made
from the current public Caddy routes, changes the guest listener to
`0.0.0.0`, points the ERP backend at guest loopback 8130, and uses guest-local
certificate paths `/etc/company-clock-tls/fullchain.pem` and
`/etc/company-clock-tls/server.key`. Keep this host-specific file out of the
repository. Copy the leaf certificate and key to those paths in the guest;
keep the CA signing key on the host. Install the root certificate at
`/etc/company-clock-tls/root-ca.crt`; the guest coordinator reads it to mount
into employee browser containers. Caddy reads only the leaf chain and key.

Install `guest/client-trust.env` as `/etc/company-clock-tls/client-trust.env` after adding the public company root to the guest OS trust store with `update-ca-certificates`. The final native-service EnvironmentFile prevents older private environment files from overriding that trust. Set `mail.tls_ca_file` in the coordinator runtime JSON to `/etc/company-clock-tls/root-ca.crt` for IMAP/SMTP.

Guest containers that call the company HTTPS names should map those names to
Docker's `host-gateway`; the guest Caddy listens on all guest interfaces and
terminates those TLS requests. Use the Compose `extra_hosts` pattern already
used by the base deployment, with the eight `TAC_CLOCK_*_HOST` variables mapped
to `host-gateway`. Native guest processes can resolve the same names to
`127.0.0.1` in the guest `/etc/hosts`. The external host DNS records remain
unchanged.

Employee desktop Chrome needs the guest root CA in its persistent NSS profile.
Set `employee_desktop_ca_file` to `/etc/company-clock-tls/root-ca.crt` and
`employee_desktop_image` to the upstream pinned Selkies desktop image in the
private runtime JSON. Coordinator Ubuntu needs `libnss3-tools` (`certutil`) to
prepare a persistent per-employee NSS database on the host; the runtime mounts
that database at Chrome's standard NSS path. This also configures independently
scheduled issue handoffs. `EMPLOYEE_DESKTOP_CA_FILE` and
`EMPLOYEE_DESKTOP_IMAGE` remain optional process overrides. Do not set a desktop fake-time clock file or inject
`LD_PRELOAD`/`FAKETIME`; the guest kernel provides the accelerated clock.

Install `guest/tac-company-clock-caddy.service` only after the guest cert files,
private Caddyfile, and environment file are in place. The service validates
the guest Caddyfile at start and exposes the same Unix admin socket used by the
desktop route manager. The host public Caddy employee-desktop forward is a
separate cutover change and is not part of this preparation.

## Host clock control

`clock_control.py` is the host clock supervisor. It requires the host's Python
libvirt bindings, SQLite (Python standard library), libvirt, and the provisioned
localhost SSH key. It never provisions a VM or changes original services.
Start is allowed only for a stopped, prepared candidate.

```sh
sudo python3 deploy/virtual-time-world/clock_control.py status
sudo python3 deploy/virtual-time-world/clock_control.py prepare
sudo python3 deploy/virtual-time-world/clock_control.py stop
sudo python3 deploy/virtual-time-world/clock_control.py initialize --host-now --rate 24
sudo python3 deploy/virtual-time-world/clock_control.py start
sudo python3 deploy/virtual-time-world/clock_control.py set-rate 24
sudo python3 deploy/virtual-time-world/clock_control.py monitor
```

Run `prepare` after the guest kernel build while the guest is still running. It
reads the build manifest, records the root filesystem UUID, and copies the
kernel and initramfs to host storage. Then use `stop` followed by `start`. The
host stores a durable SQLite guest-time/host-time/rate anchor under
`$WORLD_ROOT/config/clock-state.sqlite3` (override the root with `--root`).
Each start supplies the durable guest epoch and paired host realtime anchor as
`virtual_time_epoch_sec`/`virtual_time_epoch_nsec` and
`virtual_time_host_epoch_sec`/`virtual_time_host_epoch_nsec`, always booting the
kernel at `virtual_time=1`. The host durably records the intended world epoch and
rate before launch. Once guest-agent time and the runtime-rate sysfs control are
available, it applies the persisted rate, reconciles the guest time to the
forward projection of the durable anchor, confirms both observations, and only
then starts `agentic-company.target`. This avoids scaling the kernel's early
jiffies initialization twice. Powered-off time accrues at the last selected
rate. A successful runtime rate change is applied through
`/sys/kernel/virtual_time_rate` and then checkpoints guest-agent time. Run `monitor` while the guest is active to
checkpoint actual guest-agent time every five seconds using libvirt's event
loop. It listens for the guest-agent lifecycle event and retries only explicit
agent-not-ready errors during boot; there is no startup timeout or clock-reset
fallback. Graceful stop projects forward from its last durable guest-time sample
through shutdown at the active rate; endpoint precision is bounded by shutdown
duration. Abrupt host loss resumes from the last durable anchor; elapsed time
since that anchor is not guaranteed. Host clock rollback is refused rather than
rewinding guest time. Errors are reported without forced shutdown or kill.

Initialize exactly once before starting clock control. For the empty proof VM,
stop the stock guest, then `initialize --host-now --rate 24` explicitly seeds
it from host UTC. For a real
migration, supply the captured company clock instead with
`initialize --epoch-ns <company-epoch-nanoseconds> --host-epoch-ns <paired-host-epoch-nanoseconds> --rate <rate>`; the controller
never substitutes host UTC for a missing company epoch and never overwrites an
existing anchor. The offline rate is the rate used while the VM is powered off.
If the host loses power during a runtime rate change, `start` stays blocked
until `monitor` observes the live rate or `resolve-pending-rate --rate <rate> --epoch-ns <observed-guest-epoch> --host-epoch-ns <paired-host-epoch>`
records operator evidence for the stopped VM's applied rate and actual time.
A rate alone cannot establish the instant an interrupted rate change took effect.
Run `monitor` as a
host service or supervised process while the candidate VM is active to retain
the five-second checkpoints across controller CLI invocations.

For host and guest reboot supervision, install `clock_control.py` at
`/usr/local/lib/virtual-time-world/clock_control.py`, install
`agentic-company.target` in the guest, and install
`virtual-time-world-clock.service` into the host's systemd unit directory. The
base target starts Docker only, which supports an empty proof VM. For the company
migration, install `agentic-company.target.d/services.conf` with the existing
company unit files, `agentic-company-compose.service`, and
`company-compose-stop.py` at `/usr/local/lib/virtual-time-world/company-compose-stop`
so they are pulled in only after clock synchronization. Resolved migration Compose JSON stays under
`$WORLD_ROOT/config/migration-compose`; its pre-network-change checkpoint stays
under `$WORLD_ROOT/config/migration-compose-original`. The guest unit runs each
project with `docker compose up --pull never`. On stop it drains stateful mail,
Mailu admin, ERP frontend, and Plane worker processes, then runs
`docker compose stop --timeout -1`; it never removes containers or volumes. The
stateless experiments and controller services use Compose's init process to
forward shutdown signals.
Provisioning disables Docker's multi-user startup so applications wait for
clock readiness.
The company target uses systemd's `Upholds=` dependency to recover company
units after a failed startup dependency, while leaving the kernel clock running.
After `daemon-reload`, enable and start `virtual-time-world-clock.service`. Its
`run` action starts the prepared VM when stopped, synchronizes the guest clock,
queues the company target with systemd's nonblocking start operation, and
monitors the clock while active. A failed start request is logged and retried;
the acknowledgement means only that systemd accepted the request, not that the
applications are healthy. An unexpected guest
shutdown exits with failure so systemd restarts it from a fresh epoch derived
from the durable anchor. The libvirt domain's reboot action is `destroy`,
ensuring a guest reboot reaches the host supervisor instead of reusing the old
kernel command line. systemd's `ExecStop` requests graceful shutdown; the unit
disables SIGKILL fallback. Install and enable the unit only after candidate
preparation and explicit clock initialization.

The controller requires the prepared candidate to include both the durable
epoch and runtime-rate kernel patches, and `CONFIG_RTC_HCTOSYS` disabled. It checks the
build manifest and copied artifact hashes before booting. It does not
auto-activate a kernel or move guest data.

## Private clock API

`clock_api.py` exposes the active guest clock to the private coordinator and
dashboard. Install `virtual-time-world-clock-api.service` on the host after
placing the source beside `clock_control.py` and provision a private bearer
token at `$WORLD_ROOT/config/clock-api-token` with owner-only permissions. The
API uses a dedicated host environment with the system libvirt binding and the
project's pinned FastAPI/uvicorn dependencies. Create it without changing the
original company environment:

```sh
sudo python3 -m venv --system-site-packages "$WORLD_ROOT/host-venv"
sudo "$WORLD_ROOT/host-venv/bin/pip" install \
  --constraint servers/simulation/requirements.txt fastapi uvicorn
```

It listens only on host
`127.0.0.1:8133`; QEMU user networking exposes that host endpoint to the guest
as `http://10.0.2.2:8133`, without binding a LAN interface. Authentication is a
bearer token only; there is no UI login flow.

`GET /clock` returns `{ "speed": positive_integer, "epoch_ns": integer,
"mode": "guest-kernel" }` from libvirt guest time and the active kernel rate.
`POST /clock/speed` accepts `{ "speed": positive_integer }` with an
`Idempotency-Key` header and returns `{ "speed": integer }`. Repeating a
completed key with the same speed returns its saved response; using that key
with another speed returns 409. Reserved or uncertain operations are not
repeated automatically. Operational failures return a safe 503 to clients and
retain the traceback in host service logs.

Kernel builds and manual clock/device observations are infrastructure work.
Do not launch company experiments, benchmark episodes or Viv evaluations as
part of this stage.

## Migration boundary

Company data stays on guest-owned block storage. Do not use host directory
shares for company data: the host would generate their filesystem timestamps.
Do not point two writable deployments at one database directory. Move data only
after the guest clock/timers work and a coordinated, consistent final snapshot
can be taken. Preserve the current deployment and its volumes for rollback.

The host retains real time and handles external model authentication, transport
boundaries and the confirmed five-real-minute unread-email handoff. External
inboxes still record their own real arrival time. Internal company services use
guest time. When migrating the coordinator, set its extra business-clock rate
to one so the guest clock is not accelerated twice.

For an existing company, follow the [migration runbook](../../docs/enterprise-simulation/virtual-time-migration.md). A plain `docker compose up` starts application containers; VM creation, kernel preparation and the coordinated data migration are separate operator steps.

The coordinator runtime JSON exposes `sqlite_busy_timeout_seconds` (existing default: 30). Keep that unscaled base value; every simulation SQLite connection multiplies it by the active kernel rate. At 24×, the resulting 720 guest seconds retain a 30-real-second allowance for durable storage contention. This changes resource waiting, not the guest clock or benchmark deadlines. Health reads use a read connection, and displayed company time comes directly from the guest kernel; delayed record processing does not freeze the clock.

Clock speed is deployment configuration. Install `clock.env.example` as `$WORLD_ROOT/config/clock.env`, set `WORLD_CLOCK_RATE`, and run `sudo systemctl reload virtual-time-world-clock.service` to apply it without stopping the VM. The service uses this rate when starting a stopped VM; recovery of an already-running VM retains its durable confirmed clock state. The dashboard displays company time and has no speed editor.

Keep `mailbox_idle_renewal_seconds` at its existing 30-second base value. In kernel-clock mode the runtime multiplies it by the current kernel rate, keeping IMAP IDLE renewal at 30 real seconds. New-mail notifications still wake immediately. Transport heartbeat and reconnect intervals use the same rate at connection setup.

Keep `company_clock_observation_seconds` at its existing one-second base value; the runtime multiplies it by the active kernel rate so database checkpoints and event-queue scans run once per real second. This is a processing cadence, not a second clock: the header and business timestamps use the guest kernel, and queued events retain their scheduled timestamps. It avoids performing 24 checkpoint writes per real second on slower persistent storage.

Docker health probes are infrastructure observations, not business events. The
company Compose helper saves each image or container's baseline healthcheck and
scales its intervals, timeouts, and startup grace by the live kernel rate. It
applies the resulting fields through ordinary Compose configuration. In the
guest, after creating the company containers (or for an existing guest
deployment), run:

```sh
sudo python3 deploy/virtual-time-world/prepare-healthcheck-cadence.py \
  --config-dir /data/theagentcompany/virtual-time-world/config/migration-compose
```

Install the helper at `/usr/local/lib/virtual-time-world/prepare-healthcheck-cadence.py` in the guest. The company Compose unit runs it before startup; it reads the same kernel rate for Docker healthchecks, native service timeouts, the Mailu Dovecot proxy timeout, and the issue timer, with no separate cadence-rate configuration. After changing the rate, rerun it and apply the prepared Compose configurations to update their healthchecks.

The helper captures native effective health metadata privately, retains its
commands and retries, and scales health durations from their saved baselines.
It also scales native service timeouts and the issue timer from their saved
baselines. Repeated runs use the saved baseline rather than multiplying again.
The helper reads the active kernel rate by default (or
accepts the same rate explicitly with `--rate`), saves the native
`tac-company-issues.timer` monotonic interval privately, and installs a systemd
drop-in scaled from that saved baseline. Repeated runs do not multiply the
previous drop-in value. Recreate the affected containers through Compose to
apply their health configuration, preserving volumes. This prevents
accelerated health polling from saturating storage; business clocks, issue
deadlines and evaluator criteria are unchanged.

Use the standard Ubuntu Docker package and the standard pinned Mailu Dovecot
image. Kernel-clock behavior comes from the timed Ubuntu VM; container health
durations and Dovecot proxy timeouts are set by the company Compose helper.
