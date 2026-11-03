# Isolated company VM

## Fresh platform command

`up.sh` creates a new Ubuntu VM, installs the repository source and standard
Docker/Compose packages, bootstraps a fresh company from the supplied profile,
builds and activates the timed-kernel candidate, and starts the clock
supervisor. A normal run resumes only a world created by this command. It refuses
to adopt an unmarked VM or disk at the selected world path.

Create a private deployment profile from
[`deployment-profile.example.json`](deployment-profile.example.json), replace
the example domains, public IP, Viv integration details, and shared sandbox
password with the deployment's values, then run:

```sh
sudo install -d -m 0700 /data/theagentcompany/virtual-time-world/config
sudo install -m 0600 /path/to/deployment-profile.json \
  /data/theagentcompany/virtual-time-world/config/deployment-profile.json
sudo deploy/virtual-time-world/up.sh
```

When deliberately replacing the selected world's VM and guest storage, use the
same profile with `sudo deploy/virtual-time-world/up.sh --fresh`.

The profile pins an exact Codex CLI npm version and model. The script installs
that standard CLI and prepares the private host inference bridge; the dashboard
provides the Codex device-auth flow. No Codex login session or application
credentials are embedded in the repository. Fresh guest Compose files are
resolved under `/etc/company-workplace/compose`; the company target uses those
files with `--pull never --no-build`. The timed clock starts at 24× after the
services have been initialized at the stock rate. `clock_rate` is configurable
in the profile and defaults to 24. The complete platform defaults to a 12 GiB
guest with two vCPUs; `WORLD_MEMORY_MIB` and `WORLD_VCPUS` can override those
resource settings.

The host `tac-https.service` fronts all seven profile domains with public TLS.
Provide `public_tls.cert_file` and `public_tls.key_file` together when using an
existing deployment certificate; otherwise Caddy obtains certificates through
ACME, which requires each hostname to resolve to this host and inbound TCP 80
and 443 to reach it. The guest CA is copied only as an upstream trust root.
QEMU forwards guest HTTP/HTTPS to host loopback ports 18080/18443 and forwards
Mailu SMTP, submission, and IMAPS on the configured `mail_ports` to the
profile's local `public_ip`. The defaults are 2525, 2587, and 2993; ensure those
ports are available and configure external DNS/firewalls for the selected
protocol ports. Guest company processes use the private-CA Mailu front on local
ports 12587/12993. The public Mailu front uses the same public certificate as
host Caddy: the host path/timer helper copies the validated Caddy-managed or
profile-supplied certificate into the guest and starts the standard pinned
Mailu nginx front. The public front is independent of internal company startup,
so ACME issuance can finish asynchronously while the private front serves the
company services.

Running the command again starts the clock/API and ensures the guest company
target, Compose projects, and HTTPS frontend are active. To rebuild the selected
world from empty VM/application storage, run `up.sh --fresh`; it destroys and
removes only that world's VM, guest disks, clock anchor, and generated bootstrap
metadata. It retains the private deployment profile and any certificate/key
files referenced by that profile. It does not create backups.

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
with SSH and web traffic forwarded on host localhost. Mail protocols are
forwarded on the profile's local public IP. It does not enable domain autostart
or change existing application addresses. Resource settings can be provided
through `WORLD_VCPUS`, `WORLD_MEMORY_MIB`, `WORLD_DISK_SIZE` and
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

## Fresh workplace bootstrap and employee browser trust

`guest/bootstrap_workplace.py` reads the private profile at
`/etc/company-workplace/deployment-profile.json`. It requires the seven service
hostnames (`domains.owncloud`, `chat`, `plane`, `mail`, `erp`, `midpoint`, and
`experiments`), `public_ip`, and the explicit shared sandbox password. The
host installer supplies that profile; it does not invent domains or
credentials. The bootstrap creates a guest-local root CA and server
certificate with OpenSSL, installs the root into the guest OS trust store, and
renders Caddy configuration from the repository template. Each certificate
generation is written as a complete directory and activated with one atomic
symlink rename, so an interrupted first run can resume without a partial trust
set. The CA key remains root-only at `/etc/company-clock-tls/root-ca.key`; the
coordinator and employee browser trust the certificate at
`/etc/company-clock-tls/root-ca.crt`. Caddy reads
`/etc/company-clock-tls/fullchain.pem` and `/etc/company-clock-tls/server.key`.

The bootstrap resolves fresh ownCloud/Rocket.Chat, Plane and midPoint Compose
projects into `/etc/company-workplace/compose`, starts them at the stock 1×
guest rate for initialization, and initializes midPoint against new storage.
It uses the supplied TAC seed images and upstream application images; Plane
storage uses the upstream MinIO image instead of a locally built image. Fresh
application and service secrets persist under `/etc/company-workplace/secrets`
with owner-only permissions. It extracts the Rocket.Chat database and Plane
PostgreSQL, Redis, and upload archives from the pinned
`ghcr.io/theagentcompany/servers-api-server:1.0.0` seed image and restores them
into new Compose-owned volumes; it does not adopt an old application volume.
The stock Bitnami MongoDB service receives
`MONGODB_EXTRA_FLAGS=--setParameter ttlMonitorEnabled=false` so MongoDB's TTL
monitor does not age out seeded company records while guest time runs faster.
The separate `bootstrap_fresh_company.py` component provisions Mailu, ERPNext
and the coordinator from the same profile. After those services are ready,
`bootstrap_fresh_identity.py` imports the three native MidPoint resources,
discovers seeded Rocket.Chat and Mailu accounts, creates the 17 company users
and ownCloud projections, links the accounts, then applies the selected
department access policy with the existing ownCloud API helpers. The persistent
company target starts the resolved JSON projects from
`/etc/company-workplace/compose`.
The same fresh identity stage reconciles the seeded employee Rocket.Chat
passwords to `sandbox_password`, verifies each account's REST ID against the
source identity map, provisions the Viv account using `viv.chat_username`
(default `viv`) and `viv.email`, and writes the private
`employee-chat-credentials.json` consumed by the coordinator.

Guest Caddy serves the configured company domains and exposes the existing
admin socket at `/run/tac-https/admin.sock`. Install its unit as
`tac-https.service` so the existing target and desktop route manager use the
same lifecycle. The host's public Caddy remains the external TLS endpoint; the
guest CA is for guest services and trusted internal clients. Set
`mail.tls_ca_file` in coordinator runtime configuration to
`/etc/company-clock-tls/root-ca.crt` for IMAP/SMTP. Guest containers that call
company HTTPS names map those names to Docker's `host-gateway`; native guest
processes resolve them through guest-local loopback. External host DNS records
remain unchanged.

Employee desktop Chrome needs the guest root CA in its persistent NSS profile.
Set `employee_desktop_ca_file` to `/etc/company-clock-tls/root-ca.crt` and
`employee_desktop_image` to the upstream pinned Selkies desktop image in the
private runtime JSON. Coordinator Ubuntu needs `libnss3-tools` (`certutil`) to
prepare a persistent per-employee NSS database on the host; the runtime mounts
that database at Chrome's standard NSS path. This also configures independently
scheduled issue handoffs. `EMPLOYEE_DESKTOP_CA_FILE` and
`EMPLOYEE_DESKTOP_IMAGE` remain optional process overrides. Do not set a desktop fake-time clock file or inject
`LD_PRELOAD`/`FAKETIME`; the guest kernel provides the accelerated clock.

The guest Caddy service is installed and started by `up.sh` after the fresh
certificate, Caddyfile, and workplace services are ready. It exposes the Unix
admin socket used by the desktop route manager. The host Caddy frontend routes
each configured public hostname through the guest HTTPS forward.

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

`up.sh` initializes a new world's clock anchor from host UTC exactly once. The
offline rate is the rate used while the VM is powered off.
If the host loses power during a runtime rate change, `start` stays blocked
until `monitor` observes the live rate or `resolve-pending-rate --rate <rate> --epoch-ns <observed-guest-epoch> --host-epoch-ns <paired-host-epoch>`
records operator evidence for the stopped VM's applied rate and actual time.
A rate alone cannot establish the instant an interrupted rate change took effect.
Run `monitor` as a
host service or supervised process while the candidate VM is active to retain
the five-second checkpoints across controller CLI invocations.

`up.sh` installs the clock controller, systemd units, guest target and Compose
helper. The supervisor synchronizes guest time before starting
`agentic-company.target`, then monitors the VM. The target includes the standard
Docker/Compose projects and native company services. A normal service shutdown
drains application processes and stops Compose projects without removing their
containers or volumes. Docker's multi-user startup is disabled so applications
wait for clock readiness.

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

## Runtime cadence

The coordinator runtime JSON exposes `sqlite_busy_timeout_seconds` (existing default: 30). Keep that unscaled base value; every simulation SQLite connection multiplies it by the active kernel rate. At 24×, the resulting 720 guest seconds retain a 30-real-second allowance for durable storage contention. This changes resource waiting, not the guest clock or benchmark deadlines. Health reads use a read connection, and displayed company time comes directly from the guest kernel; delayed record processing does not freeze the clock.

Clock speed is deployment configuration. Install `clock.env.example` as `$WORLD_ROOT/config/clock.env`, set `WORLD_CLOCK_RATE`, and run `sudo systemctl reload virtual-time-world-clock.service` to apply it without stopping the VM. The service uses this rate when starting a stopped VM; recovery of an already-running VM retains its durable confirmed clock state. The dashboard displays company time and has no speed editor.

Keep `mailbox_idle_renewal_seconds` at its existing 30-second base value. In kernel-clock mode the runtime multiplies it by the current kernel rate, keeping IMAP IDLE renewal at 30 real seconds. New-mail notifications still wake immediately. Transport heartbeat and reconnect intervals use the same rate at connection setup.

Keep `company_clock_observation_seconds` at its existing one-second base value; the runtime multiplies it by the active kernel rate so database checkpoints and event-queue scans run once per real second. This is a processing cadence, not a second clock: the header and business timestamps use the guest kernel, and queued events retain their scheduled timestamps. It avoids performing 24 checkpoint writes per real second on slower persistent storage.

Docker health probes are infrastructure observations, not business events. The
company Compose helper saves each image or container's baseline healthcheck and
scales its intervals, timeouts, and startup grace by the live kernel rate. It
applies the resulting fields through ordinary Compose configuration. In the
guest, after creating the company containers, run:

```sh
sudo python3 deploy/virtual-time-world/prepare-healthcheck-cadence.py \
  --config-dir /etc/company-workplace/compose
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

For an existing world, `compact-and-move-images.sh /path/on/ssd` uses native
libguestfs sparsification and QEMU conversion/comparison with the VM and clock
supervisor stopped. It preserves company files and virtual disk capacities,
flattens the boot disk backing chain, and persists an SSD bind mount at the
existing image path. The original HDD directory is retained only until the
operator verifies startup, then must be removed. No company reset is performed.
