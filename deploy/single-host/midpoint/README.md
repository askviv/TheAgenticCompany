# midPoint identity service

This is an isolated midPoint 4.11.0 deployment with a native PostgreSQL
repository. It runs on the Mac development host at
`http://127.0.0.1:18120/midpoint/`. The deployment host can reach the same private
instance at `http://127.0.0.1:18121/midpoint/` through the reverse SSH forward
used by the scenario runner. Both addresses are loopback-only; neither is a
public service.

The Compose project is named `tac-midpoint`. Its PostgreSQL and midPoint data
volumes persist across container restarts. The server image runs as UID 1000.
Do not remove the volumes during routine restarts or connector upgrades.

## Current resources

Three native midPoint Resource objects are imported:

| Resource | OID | ConnId UID / NAME | Operations |
| --- | --- | --- | --- |
| Rocket.Chat | `57a19684-cfe8-4f21-b7b4-0fb02b5ec001` | `_id` / `username` | Read, create, update `name` and `email` |
| Mailu | `57a19684-cfe8-4f21-b7b4-0fb02b5ec002` | `email` / `username` | Read, create, update `displayed_name` |
| ownCloud | `57a19684-cfe8-4f21-b7b4-0fb02b5ec003` | `_id` / `uid` | Read, create, update `displayname` and `email` |

Each resource uses account object class `ri:User`. Immutable identifiers and
initial usernames remain weak mappings. Central display name and (where
supported) email use normal-strength mappings, so explicit central identity
changes update the linked service attribute. Rocket.Chat and Mailu receive
their create password through MidPoint's protected focus-to-account mapping.
ownCloud initial passwords are set on the native account projection under the
ownCloud object-type security policy; they are not mapped from the central
employee password. The connectors read the protected ConnId password only
while building a create request. Passwords are never projected from providers
or sent during reconciliation of an existing linked account. See
[`connectors/README.md`](connectors/README.md) for API and contract-test
coverage.

The reviewed employee roster contains 17 existing employee records. Their
Plane account IDs and email addresses are recorded in the employee extension.
All 17 employees have adopted Rocket.Chat and Mailu accounts (34 native
resource links total); Rocket.Chat correlation uses its immutable account ID.
The reviewed run is recorded in
`adoption-verification.json` (private operational report), including each
midPoint employee OID and the two linked shadow OIDs. Repeating adoption added
no links, and Sarah's native reconciliation returned HTTP 204 without changing
her Rocket.Chat UID, username, or role.
Existing Rocket.Chat and Mailu application passwords, roles, usernames, and
provider IDs are preserved during reconciliation. The initial ownCloud
inventory found no seeded employee accounts. The 17 seeded employees now have
individual ownCloud accounts provisioned through the native MidPoint shadow
and connector API, linked by their immutable ownCloud user IDs. Initial account
passwords use a resource-scoped ownCloud policy; central employee passwords
and the global password policy remain unchanged. New users have native ownCloud
home directories and no fabricated employee group or share memberships. The
shared operator account and its existing group/share state are preserved.
Individual GitLab employee accounts remain unavailable; the shared GitLab root
account is not treated as an employee account. See
`owncloud-provisioning-verification.json` (private operational report)
and `owncloud-inventory-verification.json` (private operational report).

## Verified provisioning and adoption

A fresh labelled infrastructure employee was added through midPoint's native
model API with all three resources assigned. Creation returned HTTP 201;
reconciliation returned HTTP 204 with the same three shadow IDs. Mailu IMAP
and SMTP STARTTLS login, Rocket.Chat login with the returned immutable ID, and
ownCloud WebDAV home access (HTTP 207) all passed using the assigned protected
password. See `provisioning-verification.json` (private operational report).
Two infrastructure verification employees and their target accounts are retained
with private credentials; they are not business experiments or seeded employees.

To repeat adoption after reviewed discovery, run:

```sh
python3 deploy/single-host/midpoint/adopt-accounts.py --output /private/path/adoption.json
```

The helper links only exact inventoried immutable IDs, checks current ownership,
and reads native links back. It never assigns resources or creates target accounts.
The latest repeat added zero links. The ownCloud provisioning record includes
each returned immutable UID and MidPoint shadow OID. GitLab still has no reviewed
individual employee mappings.

The custom bundles implement only the scoped updates listed above. They reject changes to account IDs, usernames, roles, authentication, and Mailu mailbox addresses. The reviewed Rocket.Chat, Mailu, and ownCloud bundles are installed in the running midPoint container, and their Resource objects are imported. Installed bundle hashes are recorded in `deploy/single-host/midpoint/connectors/deployment-verification.json`. Native resource tests and read-only post-deployment checks passed.
No account reconciliation job is scheduled: the built-in Recompute all users task
is suspended; the other default tasks are trigger scanning, validity scanning and
cleanup.

## Host and connector tunnels

The Mac connector process reaches the host services using the persistent
LaunchAgent `org.example.tac-midpoint-connectors`. It owns these forwards:

| Mac endpoint | Mini target | Use |
| --- | --- | --- |
| `127.0.0.1:13000` | `127.0.0.1:3000` | Rocket.Chat API |
| `127.0.0.1:18112` | `127.0.0.1:8112` | Mailu API |
| `127.0.0.1:8092` | `127.0.0.1:8092` | ownCloud OCS API, preserving its trusted host |
| Mini `127.0.0.1:18121` | Mac `127.0.0.1:18120` | Private midPoint route for the scenario service |

Install or refresh the LaunchAgent with the private SSH key path:

```sh
python3 deploy/single-host/midpoint/install-connectors-tunnel.py \
  --ssh-key /path/to/private/ssh-key
```

The installer enables strict host-key checking and keeps SSH logs under
`~/.local/share/tac-midpoint/`. To stop only this tunnel:

```sh
launchctl bootout gui/$(id -u)/org.example.tac-midpoint-connectors
```

The reverse forward exposes the scenario API route only on the host's loopback
interface. Scenario API credentials remain in the Mac's private configuration
and are not stored in this repository.

## Private configuration

The Compose environment file and secret files are outside the repository in
`~/.config/theagentcompany/`. The current files include:

- `midpoint-4.11-poc.env`, which contains paths to Docker secrets;
- `midpoint-4.11-poc-admin-password` and `midpoint-4.11-poc-db-password`;
- `connector-secrets.json`, containing application connector credentials.

Keep these files private and mode `0600`. The resource importer reads connector
credentials into memory and sends protected configuration values to midPoint;
resource XML templates contain no credentials. Never print connector secrets,
employee passwords, or provider responses in diagnostics.

## Start and stop

Start PostgreSQL and midPoint without rerunning initialization:

```sh
docker compose \
  --env-file ~/.config/theagentcompany/midpoint-4.11-poc.env \
  -f deploy/single-host/midpoint/compose.yml up -d
```

Check the service health at
`http://127.0.0.1:18120/midpoint/actuator/health`. A healthy response is
`{"status":"UP"}`.

Stop the containers while retaining their data:

```sh
docker compose \
  --env-file ~/.config/theagentcompany/midpoint-4.11-poc.env \
  -f deploy/single-host/midpoint/compose.yml stop
```

The `initialize` profile runs Evolveum's `init-native` and repository/audit
`run-sql --create` commands. Use it only once against a new empty PostgreSQL
volume. Do not run it on the active repository.

## Build and import connectors

Build the pinned Evolveum SCIMREST-based bundles with:

```sh
deploy/single-host/midpoint/connectors/build.sh
```

The script verifies pinned upstream artifact digests and produces ignored JARs
under `connectors/build/`. Copy rebuilt JARs into the persistent
`/opt/midpoint/var/connid-connectors/` directory, then restart midPoint so it
discovers the new bundles. The exact build commands, test script, API
projections, and supported operations are in the connector README.

To import or replace all three Resource objects from their XML definitions:

```sh
ROCKETCHAT_BASE_ADDRESS=http://host.docker.internal:13000/api/v1 \
MAILU_BASE_ADDRESS=http://host.docker.internal:18112/api/v1 \
python3 deploy/single-host/midpoint/connectors/configure_resources.py \
  --with-owncloud --replace-existing
```

This updates Resource and password-policy configuration only; it does not
assign resources or create target accounts. For ownCloud, initial account
credentials belong to the native account projection and are validated against
the resource-scoped policy, not the central employee password policy. The
seeded roster's provisioned UIDs and MidPoint shadow OIDs are recorded in
`owncloud-provisioning-verification.json` (private operational report).

## Capacity

Evolveum recommends roughly 4 GB for the midPoint JVM and 2 GB for PostgreSQL,
before Docker and host overhead. The current proof-of-concept Compose limits
are lower (3 GB JVM maximum, 4 GB midPoint container, and 1.5 GB PostgreSQL)
because Docker has limited free memory alongside other active services. This
is suitable for a small functional proof, not a production capacity claim.
Measure `docker stats` during startup, reconciliation, and normal use before
choosing long-term placement or limits.

## References

- [Evolveum container deployment](https://docs.evolveum.com/midpoint/get-started/install/containers/)
- [midPoint 4.11.0 release](https://docs.evolveum.com/midpoint/release/4.11.0/)
- [SCIMREST framework](https://docs.evolveum.com/connectors/scimrest-framework/)
- [midPoint 4.11 mapping strengths](https://docs.evolveum.com/midpoint/reference/support-4.11/expressions/mappings/)
- [ownCloud 10.16 Provisioning API](https://owncloud.dev/docs/server/10.16/developer_manual/core/apis/provisioning-api.html)
