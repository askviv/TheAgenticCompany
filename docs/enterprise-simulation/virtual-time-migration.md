# Whole-company virtual-time migration runbook

This runbook moves an existing company deployment into a prepared Ubuntu KVM
guest. It covers source inventory, private Compose configuration, image and
data transfer, the paired clock anchor, cutover, and rollback. It does not
provision the VM, build or select its kernel, create its persistent data disk,
configure external TLS or transport, or start the clock supervisor. Complete
those steps separately before migrating application data.

The guest must have its persistent company data filesystem mounted before
Docker starts, and Docker/containerd storage must be on that guest-owned
filesystem. Confirm capacity for the source data, all selected images, and
working space. Do not use a host directory share for company files or Docker
volumes.

## Migration rules

- Keep the source deployment and its volumes intact for rollback. Do not run
  source and guest as simultaneous writers to the same logical company data.
- Do not start guest company containers until their images, Compose
  configuration, bind data, named-volume metadata and payloads, and clock
  anchor are verified.
- Capture all state from the same source deployment: Compose project files and
  overrides, environment inputs, containers, image IDs, named volumes, bind
  mounts, dynamic employee desktop containers and their profiles, and the
  company clock state.
- Treat Docker inspect output and resolved Compose JSON as private. They can
  contain credentials and other environment values. Write them only to
  owner-only files, never print them, and do not commit them.
- Do not use `docker compose down`, volume removal, `rsync --delete`, or an
  automatic fallback to an incomplete source. Keep original data until the
  guest deployment is accepted.

The commands below use these operator-supplied variables:

```sh
set -euo pipefail
export WORLD_ROOT=/data/theagentcompany/virtual-time-world
export COMPANY_ROOT=/data/theagentcompany
export GUEST_SSH_TARGET=guest
```

`GUEST_SSH_TARGET` is an SSH alias configured by the operator for the prepared
guest. Supply its key and host-key policy through the local SSH configuration;
do not put addresses, usernames, private keys, or credentials in this runbook.
The examples assume Bash, Docker, Docker Compose v2, `jq`, `rsync`, SSH, and
Python are available on the source host. Run privileged Docker commands with
the site's normal authorized mechanism.

## 1. Prepare private inventory and target configuration

Create owner-only migration directories and keep command tracing disabled:

```sh
umask 077
install -d -m 0700 "$WORLD_ROOT/config/migration-compose-original"
install -d -m 0700 "$WORLD_ROOT/config/migration-compose"
install -d -m 0700 "$WORLD_ROOT/images"
```

For each existing Compose project, run `docker compose --profile '*' config --format json`
with the same `-f` files, `--env-file` options, and environment used by that
project. Write its resolved configuration directly to a mode-0600 private file;
do not send rendered configuration to the terminal or logs. Preserve a private
original checkpoint before applying guest-specific changes:

```sh
docker compose -f compose.yml -f site.override.yml \
  --env-file private.env --profile '*' config --format json \
  > "$WORLD_ROOT/config/migration-compose-original/project.json"
chmod 0600 "$WORLD_ROOT/config/migration-compose-original/project.json"
cp "$WORLD_ROOT/config/migration-compose-original/project.json" \
  "$WORLD_ROOT/config/migration-compose/project.json"
chmod 0600 "$WORLD_ROOT/config/migration-compose/project.json"
```

Apply and review only the deployment-specific guest changes in the target JSON,
such as guest-local service endpoints and host-gateway mappings. Include profiled maintenance tools such as ERPNext `site-admin` even when they have no running container; the existing adapters invoke them on demand. Preserve
project names, service definitions, volume names, restart policies, health
checks, and secret references. Compare the private original and target files
before proceeding. Store one resolved JSON file per project in
`$WORLD_ROOT/config/migration-compose`; this is the directory consumed by the
guest company Compose unit. The target Compose configuration is not a VM
provisioner and does not create the guest disk, clock service, or TLS boundary.

For containers whose main application process does not handle shutdown signals
reliably as PID 1 (including Node-based services), set Compose `init: true` in
the reviewed target configuration so a small init process forwards signals to
the application. Shell and Python startup wrappers that own child daemons need
an application-native drain first, followed by the wrapper's documented
shutdown signal; do not assume signaling a child will stop its wrapper or
preserve state by itself.

Capture the source container inventory privately. Include stopped company
containers as well as running containers, and explicitly include dynamic
employee desktop containers and any other company-owned containers that are
not represented by a Compose project. Exclude unrelated host workloads. Inspect
mounts and labels to determine the full company-owned set before writing the
inventory:

```sh
docker inspect CONTAINER_ID_1 CONTAINER_ID_2 \
  > "$WORLD_ROOT/config/company-containers.inspect.json"
chmod 0600 "$WORLD_ROOT/config/company-containers.inspect.json"
```

The file contains sensitive environment values. Keep it private and use only
the `.Image` IDs, mount metadata, restart policies, and labels needed for the
migration. Do not use `docker image ls` as the complete source inventory:
untagged images can be omitted from that listing.

Capture exact image IDs and tag references from those inspected containers.
Also include any separately identified company image needed after migration,
such as the employee desktop base image. Keep a private manifest of IDs and
references; do not export every image on the host. The source Docker daemon and
all source images remain intact for rollback.

For an inspect file containing only the selected company containers, derive the
IDs without printing the inspect data:

```sh
jq -r '.[].Image' "$WORLD_ROOT/config/company-containers.inspect.json" \
  | sort -u > "$WORLD_ROOT/config/company-image-ids.txt"
chmod 0600 "$WORLD_ROOT/config/company-image-ids.txt"
```

Review that private list against the selected container set and add any
separately required company image IDs before saving.

Capture Docker volume metadata for every company container mount with the
native `docker volume inspect` command:

```sh
docker volume inspect VOLUME_NAME_1 VOLUME_NAME_2 \
  > "$WORLD_ROOT/config/company-volumes.inspect.json"
chmod 0600 "$WORLD_ROOT/config/company-volumes.inspect.json"
```

Review each volume's exact `Name`, `Driver`, `Options`, and `Labels`, and map
each container mount to its volume or bind source. Stop and resolve any
non-local driver or unreviewed options rather than guessing how to recreate it.
For local bind volumes, verify the `Options.device` source path is included in
the company-tree copy. Do not copy the unused Docker `_data` mountpoint for a
bind volume; copy its actual bind source once with the company tree. Before
creating a bind-backed guest volume, ensure its exact `Options.device` path
already exists in the guest; if no pre-copy created it, run the final company
tree copy below before creating that volume.

## 2. Export and load selected images

Export only the selected company image IDs. A retained archive on the source
host supports rollback and avoids relying on future registry availability:

```sh
docker image save $(cat "$WORLD_ROOT/config/company-image-ids.txt") \
  > "$WORLD_ROOT/images/company-images.tar"
chmod 0600 "$WORLD_ROOT/images/company-images.tar"
```

Load directly into guest Docker over SSH. This streams the archive and avoids
creating a second large archive on the guest:

```sh
ssh "$GUEST_SSH_TARGET" 'sudo -n docker image load' \
  < "$WORLD_ROOT/images/company-images.tar"
```

Verify each expected ID with `docker image inspect` on the guest, including
untagged IDs. Verify the guest still has no company containers:

```sh
while IFS= read -r image_id; do
  ssh "$GUEST_SSH_TARGET" sudo -n docker image inspect "$image_id" >/dev/null
done < "$WORLD_ROOT/config/company-image-ids.txt"
guest_container_ids=$(ssh "$GUEST_SSH_TARGET" sudo -n docker ps -aq)
test -z "$guest_container_ids"
```

Do not delete the source archive or source images after loading. A failed image
ID check must be resolved before application data is started in the guest.

## 3. Create guest volumes and copy data

On the guest, create a Docker volume for each captured volume, preserving its
exact driver, options, and labels. For a fresh guest, use the native command
shape:

```sh
docker volume create --driver DRIVER \
  --label LABEL_KEY=LABEL_VALUE \
  --opt OPTION_KEY=OPTION_VALUE \
  VOLUME_NAME
```

Run that command in the guest shell. Repeat the label and option flags for
every captured entry. Omit a flag only
when that corresponding source field is absent. Then run `docker volume inspect`
on the guest and compare every name, driver, option, and label with the private
source capture. If a volume already exists, inspect and compare it; do not
silently accept mismatched metadata. Do not create application containers
during this phase.

Copy the company tree only after source writers have been quiesced for the
final pass. An earlier live pre-copy can reduce downtime, but it is not a
consistent database snapshot and never replaces this final cold copy. Stop
native actors, coordinators, timers, schedulers, and all company containers;
record original restart policies and service enablement privately so the source
can be restored. Confirm no source writer restarts before beginning the final
copy.

The final tree copy preserves ownership, ACLs, xattrs, hardlinks, sparse files,
and checksums, and does not delete destination entries:

```sh
rsync -aHAXS --numeric-ids --checksum --stats --no-D \
  --rsync-path='sudo -n rsync' \
  --exclude='/virtual-time-world/***' \
  --exclude='/enterprise/backups/***' \
  --exclude='/enterprise/actors/cache/***' \
  "$COMPANY_ROOT/" \
  "$GUEST_SSH_TARGET:$COMPANY_ROOT/"
```

Exclude `enterprise/actors/cache` only after confirming that retained actor
virtual environments, `.pth` files, and symlink targets do not refer to it.
Keep actor state, secrets, source code, and virtual environments. Exclude
`enterprise/backups` only while its original copy is retained on the source and
the migration plan explicitly keeps those backups host-side; include it in the
guest copy if guest-side backups are required. `--no-D` skips device and
special files; inventory such entries before the copy and confirm they are
recreatable runtime sockets rather than company data. Keep rsync output private
if it contains source paths. Do not add `--delete`: review unexpected
guest-only entries separately rather than deleting them as part of the
migration.

For each default local named volume, copy its source Docker mountpoint payload
to the inspected guest mountpoint sequentially, using the same preservation
flags and a private progress record:

```sh
rsync -aHAXS --numeric-ids --checksum --stats --no-D \
  --rsync-path='sudo -n rsync' \
  "$SOURCE_MOUNTPOINT/" \
  "$GUEST_SSH_TARGET:$GUEST_MOUNTPOINT/"
```

Do not run large tree and volume copies concurrently on a seek-limited source
disk. For bind volumes, verify that the target `Options.device` points to the
corresponding copied company-tree path and record the bind as covered by that
copy; do not rsync the empty Docker volume mountpoint. Reconcile failures and
verify every payload completed before proceeding. Keep progress files private.

There must still be no guest containers after all transfers:

```sh
guest_container_ids=$(ssh "$GUEST_SSH_TARGET" sudo -n docker ps -aq)
test -z "$guest_container_ids"
```

Compare the guest volume metadata and each image ID against their source
manifests. Review rsync exit status and statistics for both the final tree and
every volume. `--checksum` checks regular-file content; it does not replace
application-specific database consistency checks. Do not treat a live
pre-copy, a successful image load, or a matching file count as a final
consistency check.

## 4. Seed the paired clock and start the guest workload

At cutover, record the company's logical epoch `G` and host realtime epoch `H`
as a pair, using the authoritative source clock and host clock observation.
Capture this pair at the coordinated quiesce boundary; do not substitute the
host's current time for the company's logical time. Record the chosen powered-
off rate and keep the pair private with the migration record.

Initialize the host clock controller exactly once with that captured pair, then
start the prepared guest through the clock controller:

```sh
sudo python3 deploy/virtual-time-world/clock_control.py initialize \
  --epoch-ns "$COMPANY_EPOCH_NS" \
  --host-epoch-ns "$PAIRED_HOST_EPOCH_NS" \
  --rate "$OFFLINE_RATE"
sudo python3 deploy/virtual-time-world/clock_control.py start
```

Do not use `initialize --host-now` for a real company migration. Confirm the
controller reports the expected epoch and rate and that it has synchronized
the guest before it queues `agentic-company.target`. The Compose unit runs the
resolved per-project JSON files with `docker compose up -d --pull never`; it
starts the existing workloads but does not provision the VM or repair missing
images or data.

Only after the guest is synchronized and the application state is verified
should the operator switch external routes to the guest. Retain the source
deployment, source Docker volumes, private source manifests, image archive, and
clock snapshots until the migration is accepted.

## 5. Rollback and writes after cutover

Before guest applications accept writes, rollback is to keep or restore the
source deployment as the sole writer, using its preserved data and saved
restart/service state. Stop the guest gracefully first; never start both
deployments against one writable data directory.

After guest applications have accepted writes, the guest is no longer a
disposable copy. Do not simply restart the stale source deployment or reverse
the filesystem rsync: databases, mail, and application state require an
application-consistent restore or reconciliation. Quiesce guest writers, take
a consistent guest snapshot, and reconcile its new writes into the chosen
authoritative deployment before resuming service. Preserve the paired clock
anchor and advance it monotonically; do not reinitialize from host time or an
older snapshot. If no validated reverse/reconciliation procedure exists, keep
the guest authoritative and leave the stale source stopped until one is
prepared.

Do not remove source volumes, source images, the retained archive, or rollback
snapshots until the chosen deployment is accepted and the rollback plan for
post-cutover writes is closed.
