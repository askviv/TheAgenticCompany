# Selkies employee desktop image

The runtime uses the official Selkies desktop `2.1.0-ubuntu26.04` image with
`netcat-openbsd`, `procps`, `socat`, and `x11-utils` added for local port and
process/display checks and a narrowly scoped Chrome DevTools relay. The runtime
explicitly sets `SELKIES_MODE=websockets`; that transport uses the proxied HTTP
port only, so it does not require externally reachable ICE/UDP or TURN ports.
Chrome listens on
container loopback at `9222`; `socat` accepts on `9223` inside the employee's
dedicated Docker bridge network and forwards only to that loopback listener.
The host publishes the relay port on `127.0.0.1` only. CDP is not exposed to
the employee browser or public network.

Coordinator bootstrap builds the local image during installation and after
source updates. It skips the build when the existing image's Dockerfile digest
matches the checked-out Dockerfile. If the build fails, bootstrap warns and
continues core company setup; an on-demand desktop start reports the missing
image. Rerunning bootstrap repairs a missing or stale image once Docker can
reach the pinned base registry.

The Dockerfile pins the official Selkies image tag rather than tracking its
mutable `latest` or `main` tags. The per-employee profile volume, generated
credentials, and container survive `stop_session`; stopping never removes
Docker resources or browser data. Generated Basic credentials are held in
private coordinator state and injected by the Caddy route only for its matching
employee. The dashboard receives neither credentials nor a password-bearing
URL and is intentionally available without operator sign-in.

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

The coordinator service must retain its existing local Docker daemon access.
No Docker socket is mounted into an employee desktop or exposed on a network.
