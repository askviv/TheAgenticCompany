# Native application connectors

These ConnId bundles use Evolveum's SCIMREST framework with API-specific
search and create behavior for Rocket.Chat 5.3, Mailu 2024.06, and ownCloud
10.16. They are installed in the local midPoint 4.11 instance as native
Resource objects.

| Connector | UID / NAME | Read behavior | Create behavior | Update behavior |
| --- | --- | --- | --- |
| Rocket.Chat | `_id` / `username` | `users.list`, `users.info`, username/email/UID search; projects only `_id`, `username`, `name`, `email`, `active`, `type`, `roles` | `users.create`; accepts username, name, email, password, and default user role | `users.update`; allows only `name` and `email` |
| Mailu | `email` / `username` | `GET /user` and `GET /user/{email}`; excludes password hashes and global-admin fields | `POST /user`; maps ConnId NAME to the mailbox email and uses the assigned protected password, without global-admin privileges | `PATCH /user/{email}`; allows only `displayed_name` |
| ownCloud | `_id` / `uid` | OCS user list and detail endpoints; projects only immutable ID, account name, display name, email, and enabled state | OCS form-encoded user create; service credentials and the new user's password are separate | OCS `PUT /users/{uid}`; allows only `displayname` and `email`, one API request per field |

All three implement read, create, and narrowly scoped identity updates. Rocket.Chat updates only `name` and `email`; Mailu updates only `displayed_name` (mailbox email is the immutable UID); ownCloud updates only `displayname` and `email`. Unsupported additions, removals, credentials, usernames, roles, service IDs, or mailbox renames fail before a provider request. Provider passwords remain unreadable and are create-only. Resource mappings keep account identifiers and initial usernames weak; agreed central identity attributes use normal strength so explicit central changes can flow after initial adoption. There are no automatic delete operations. The reviewed bundles and Resource objects are installed in the running MidPoint 4.11 instance. The ownCloud Resource references a narrowly scoped account-type password policy; the global employee password policy is unchanged. See the [deployment README](../README.md) and ownCloud provisioning verification (private operational report).

## Build and install

`build.sh` retrieves pinned official SCIMREST 0.2.0 and ConnId 1.7.0.0-M0
artifacts and verifies their SHA-256 digests before compiling the application
connectors. Build outputs are ignored and are not committed:

```sh
deploy/single-host/midpoint/connectors/build.sh
```

Install the output JARs into `/opt/midpoint/var/connid-connectors/` in the
persistent MidPoint home and restart the MidPoint container. The installed
bundle digests can be checked with `shasum -a 256` against the build output.

The ownCloud connector includes a local HTTP contract test for OCS list/detail
projection, URL-encoded IDs, enabled-value validation, missing users, malformed
responses, HTTP failures, create form encoding, service authentication,
already-existing-account handling, and the scoped update/readback contract:

```sh
deploy/single-host/midpoint/connectors/owncloud/test-create.sh
```

Rocket.Chat and Mailu create/update operations have a combined ConnId facade
contract test. It sends requests to local mock HTTP endpoints and verifies
create serialization plus the exact update fields, protected authentication,
immutable account IDs, rejection of credential fields, and repeat updates. The
standalone harness does not include midPoint's Groovy runtime, so script-backed
provider search/readback is not exercised by this test:

```sh
deploy/single-host/midpoint/connectors/test-create-contract.sh
```

This test uses a local mock server; it does not create a live application
account. No framework-level integration test is a substitute for checking
resource discovery against each configured provider.

## Configure native Resources

Resource templates are maintained in the parent `midpoint/` directory. The
private importer adds app credentials in memory and sends protected values to
midPoint:

```sh
ROCKETCHAT_BASE_ADDRESS=http://host.docker.internal:13000/api/v1 \
MAILU_BASE_ADDRESS=http://host.docker.internal:18112/api/v1 \
python3 deploy/single-host/midpoint/connectors/configure_resources.py \
  --with-owncloud --replace-existing
```

Connector credentials live in `~/.config/theagentcompany/connector-secrets.json`
with mode `0600`. Do not place credentials in resource XML, source code, logs,
or documentation.
