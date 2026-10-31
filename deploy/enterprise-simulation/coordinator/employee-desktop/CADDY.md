# Trusted HTTPS for employee desktops

The coordinator can use `servers.simulation.employee_desktop_proxy.CaddyEmployeeDesktopProxy`
to register a route on the existing HTTPS site. Set `viewing_origin` when the
site origin is already configured; otherwise the helper discovers the unique
hostname attached to the existing `127.0.0.1:8110` experiments route. Leave
the admin socket at `/run/tac-https/admin.sock`.

The helper talks to Caddy only through that permissioned Unix socket. It adds
one identified route per profile employee, matching only that hostname and
`/employee-desktops/<opaque-profile-hash>/...`; its upstream is the inspected
IPv4 or IPv6 address of that employee's container on its dedicated Docker
bridge at port 8080. The web port is not published on the host, so an exited
container cannot leave a Caddy route pointing at a reused localhost port.
Caddy's existing routes and Caddyfile are preserved. Updates use Caddy ETags
and retry config conflicts. Caddy itself terminates the site's existing
trusted TLS certificate. Desktop containers receive host mappings derived
from active Caddy host matchers and listener addresses, so proxied company
sites resolve through the existing listener. Other DNS names stay with
Docker's normal resolver.

The dashboard is intentionally open without operator sign-in, within the
existing LAN HTTPS boundary. Each exact employee route sets that employee's
Selkies Basic Authorization header only on the container-bound request. The
generated password remains in private coordinator state and Caddy's protected
in-memory configuration; it is never returned for the dashboard grid or
placed in a URL. The browser sees no Basic Auth prompt, so multiple same-origin
iframes can load their streams together. The CDP endpoint remains published on
host loopback only and is not routed through Caddy.

Before creating a container, set `SELKIES_SUBFOLDER` to
`CaddyEmployeeDesktopProxy.subfolder_for(employee_id)` and
`SELKIES_ENABLE_HTTPS=false` so Selkies serves its own HTTP endpoint behind
Caddy TLS. Set `SELKIES_FRAME_ANCESTORS='self'` so only the same-origin
dashboard can embed the stream. Do not publish port 8080 to the host. Register
a route only after the desktop passes its readiness checks; remove the exact
route before stopping the container. The coordinator desktop-list endpoint
returns URLs for healthy running sessions and re-registers exact routes after
a Caddy restart. It does not start stopped desktops. The helper intentionally
does not rewrite or reload the host Caddyfile.

No live Caddy or Docker host was available for integration validation in this
change. The API path shape and ETag behavior follow Caddy's documented admin
API; tests exercise a synthetic API contract only. In particular, verify that
the host running Caddy can route to Docker bridge addresses in the deployment
environment before enabling the feature. Docker Desktop on macOS may not expose
container bridge addresses directly to host processes.
