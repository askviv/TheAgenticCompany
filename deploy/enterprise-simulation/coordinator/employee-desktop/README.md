# Employee desktop image

Coordinator bootstrap pulls the upstream Selkies desktop image
`ghcr.io/selkies-project/selkies/desktop:2.1.0-ubuntu26.04`. It does not build
or modify a desktop image. The versioned upstream tag is used instead of a
mutable `latest` or `main` tag.

The coordinator supplies the employee session's Selkies configuration,
credentials, isolated network, persistent profile volume, and optional
Chrome trust database when it creates the container. Coordinator Ubuntu needs
`libnss3-tools` (`certutil`) to add the configured company root to a persistent
per-employee NSS database mounted at Chrome's standard trust path. Do not
disable certificate checks or otherwise bypass TLS validation.

Chrome binds its DevTools endpoint to container loopback. The coordinator uses
Ubuntu's `systemd-socket-proxyd` with `NetworkNamespacePath` to accept the CDP
connection on a dynamically allocated host loopback port and forward it to
`127.0.0.1:9222` in that employee's container network namespace. The CDP port
is not published by Docker, and the proxy stops with the managed employee
session.

The upstream image is provisioned during coordinator bootstrap. If the pull
fails, bootstrap reports that employee desktops are unavailable until the
image can be pulled. Rerunning bootstrap retries the pull. Existing running
containers and their profile data are not replaced by this operation.

The coordinator service retains its existing local Docker daemon access. No
Docker socket is mounted into an employee desktop or exposed on a network.

## Coordinator settings

Configure the module with the existing `company-identities.json` as its sole
employee identity source and a private child directory under the coordinator's
state root. The manager discovers the existing experiments HTTPS hostname
through Caddy's admin Unix socket and installs an exact route for each healthy
running session. `GET /company/desktops` reconciles these routes after a Caddy
restart without starting stopped sessions. The dashboard is open within the
existing LAN HTTPS boundary; Caddy supplies each employee's stored Selkies
credential only to that employee's HTTP/WebSocket upstream. When creating a
desktop container, it derives exact hostnames and listener addresses from the
active Caddy route configuration and adds those names to the container's host
mapping. Other browser DNS lookups continue through Docker's configured DNS.

### Chrome sandbox

Employee desktops launch the installed Chrome binary with its sandbox enabled,
without Selkies' automatic `--no-sandbox` fallback. The Docker seccomp profile
`../employee-desktop-seccomp.json` retains Moby's default policy and permits
`clone`, `unshare` and `setns` for Chrome's nested namespace sandbox. It adds no
container capabilities and does not use privileged mode. The baseline is
https://github.com/moby/profiles/blob/4b8214fc4971e06172938a1f1554ccff6647ee80/seccomp/default.json.
Existing desktop containers are replaced on their next use to apply the immutable
Docker security setting; their named employee home volumes are retained.
Chrome's packaged SUID helper is restored to root ownership and mode 4755.
Sandbox startup errors fail desktop startup instead of disabling isolation.
