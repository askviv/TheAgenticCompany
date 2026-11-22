# Local email setup

For company chat webhooks to a local receiver, see [Local webhooks](LOCAL_WEBHOOKS.md).

The company uses Mailu for employee mailboxes. A local assistant can use smtp4dev
as its SMTP capture server. Mail routing is configured by recipient address; it
does not require the assistant to be Viv or require smtp4dev to run on a Mac.

## Choose the addresses

Set these values in your private deployment profile:

| Setting | Purpose |
| --- | --- |
| `domains.mail` | Public hostname of the company's Mailu server |
| `mail_ports.submission` | Authenticated Mailu submission port, normally `2587` |
| `viv.email` | Assistant's email address; this existing configuration key can identify any assistant |
| `viv.smtp_host`, `viv.smtp_port` | Assistant SMTP destination as reachable **from the company VM** |
| `viv.api_base_url` | smtp4dev settings API as reachable from the configuration host |

The current profile section is named `viv`; changing its address and endpoints
selects a different assistant. See
[`deployment-profile.example.json`](../deploy/virtual-time-world/deployment-profile.example.json)
for the complete profile. Keep the actual profile and credentials outside Git.

For an SMTP destination that is directly reachable from the VM, use its actual
hostname and port. There is no need for an SSH tunnel. Configuring this smtp4dev
relay is only applicable when smtp4dev is the assistant's local mail server;
other mail servers use their own native outbound SMTP configuration.

## Company to assistant

Deployment provisions Mailu's exact-recipient transport for the configured
assistant address. Mailu delivers that recipient to the configured assistant
SMTP destination. Incoming messages remain visible in smtp4dev; the company does
not poll the assistant's inbox to confirm delivery. SMTP acceptance is the
outbound delivery boundary.

## Assistant to company

Configure smtp4dev's **native relay settings**:

| smtp4dev setting | Value |
| --- | --- |
| Relay SMTP server | Configured `domains.mail` |
| Relay SMTP port | Configured `mail_ports.submission` |
| TLS mode | `StartTls` |
| Login/password | Mailu's managed reply-relay service account |
| Sender address override | Empty, preserving the original sender |
| Automatic relay expression | Company-domain recipients, excluding the assistant's own address |

### Apply with Docker Compose

Use the repository's [native relay override](../deploy/single-host/smtp4dev-bridge/local-email.compose.yml)
with the Compose file that owns your existing `smtp4dev` service. Copy
[the environment example](../deploy/single-host/smtp4dev-bridge/local-email.env.example)
outside the repository, fill in your deployment's values and managed relay
credentials, and protect it with `chmod 600`.

```sh
docker compose --env-file /path/to/private/local-email.env \
  -f /path/to/existing/compose.yaml \
  -f deploy/single-host/smtp4dev-bridge/local-email.compose.yml \
  up -d --no-deps smtp4dev
```

The override uses smtp4dev's built-in `RelayOptions` environment settings. It
recreates only smtp4dev, preserving the image, port mappings, network and mailbox
volume from the original Compose file. Do not add `--remove-orphans` or use
`down -v`: other services and stored mail are unrelated to this update. Keep any
existing `AutomaticEmails` list empty if only the domain expression should relay.

Values supplied through environment variables are locked in smtp4dev's settings
API. Reapply Compose to change them. Where settings are not environment-locked,
the existing `configure_reply.py --profile /path/to/private/deployment-profile.json
--base-url http://reachable-smtp4dev-api` configurator can apply the same native
settings from the machine holding the managed Mailu credentials.

Credentials must remain outside Git. Do not print the smtp4dev Server API response,
which can include the relay password. The managed relay account is limited to
the configured assistant sender identity. Employee recipients must exist in
Mailu; their native mailbox listeners receive new mail and wake their agents.
Other recipients stay captured in smtp4dev. Applying this configuration does not
send mail or retry historical mail.

## smtp4dev on a developer's laptop

If smtp4dev listens only on laptop loopback, the company VM cannot reach that
listener directly. Use the existing SSH reverse tunnel; do not expose the
smtp4dev settings API publicly. For an existing Docker smtp4dev service:

```sh
python3 deploy/single-host/smtp4dev-bridge/install-mac.py \
  --ssh-key /path/to/existing/ssh-key \
  --mini user@company-host \
  --direct --smtp-port 9170 --api-port 9167 \
  --label-prefix your.organisation
```

This installs user LaunchAgents that reconnect the tunnel automatically. It
reuses the existing smtp4dev service and does not create a replacement mailbox.
The host-side SMTP socket/proxy is described in the
[bridge deployment guide](../deploy/single-host/smtp4dev-bridge/README.md).
The settings API tunnel is for configuration, not continuous inbox polling.

### Optional SSH return path for Docker Desktop

If the laptop can reach Mailu but its Docker containers cannot reach the LAN,
the same SSH connection can forward submission back to the company host:

```sh
python3 deploy/single-host/smtp4dev-bridge/install-mac.py \
  --ssh-key /path/to/existing/ssh-key --mini user@company-host \
  --direct --smtp-port 9170 --api-port 9167 --label-prefix your.organisation \
  --reply-listen-port 19127 --reply-smtp-host company-host --reply-smtp-port 2587
```

Set `SMTP4DEV_RELAY_PORT=19127` in the private environment file. Keep
`SMTP4DEV_RELAY_HOST` equal to Mailu's verified TLS hostname. Set
`SMTP4DEV_RELAY_GATEWAY_IP` to the address returned inside your container by
`getent ahostsv4 host.docker.internal`, and add the optional
[Docker gateway override](../deploy/single-host/smtp4dev-bridge/local-email.gateway.compose.yml)
to the Compose command. This maps that TLS hostname to the Docker-to-laptop
gateway for this container only. SSH then carries the connection to Mailu;
STARTTLS still verifies Mailu's hostname and certificate. No global DNS change,
privileged container, TLS bypass or custom mail forwarder is used.

### Current local installation

| Component | Address |
| --- | --- |
| MacBook smtp4dev SMTP | `127.0.0.1:9170` |
| MacBook smtp4dev UI/API | `http://127.0.0.1:9167` |
| Mini tunnel SMTP listener | `127.0.0.1:19125` |
| Mini SMTP bridge | Port `19126`, reachable from the company VM as `10.0.2.2:19126` |
| Mini settings API tunnel | Port `19157`, reachable from the company VM as `http://10.0.2.2:19157` |
| MacBook return submission forward | `127.0.0.1:19127` → Mini `192.168.0.200:2587` |
| smtp4dev return destination | `mail.mini.askviv.co:19127`, via Docker host gateway, authenticated STARTTLS |
| Company recipient domain | `example.com` |
| MacBook tunnel LaunchAgent | `~/Library/LaunchAgents/co.askviv.tac-smtp4dev-tunnel.plist` |

The local relay forwards company-domain recipients and excludes the configured
assistant address to prevent a delivery loop. The private local Compose files
are under `~/.local/share/tac-smtp4dev/`. Reapply them with:

```sh
docker compose -p bsktpay \
  --env-file "$HOME/.local/share/tac-smtp4dev/local-email.env" \
  -f "$HOME/.local/share/tac-smtp4dev/smtp4dev.compose.json" \
  -f "$HOME/.local/share/tac-smtp4dev/local-email.compose.yml" \
  -f "$HOME/.local/share/tac-smtp4dev/local-email.gateway.compose.yml" \
  up -d --no-deps smtp4dev
```

The relay password is stored only in the private environment file and smtp4dev's
container configuration. No mailbox contents or credentials belong in Git.

Upstream references: [smtp4dev relay settings](https://github.com/rnwood/smtp4dev/blob/master/Rnwood.Smtp4dev/appsettings.json).

## Delivery verification

The current installation was checked with explicitly labelled connection messages:
Mailu delivered the outbound message into local smtp4dev, smtp4dev recorded a
successful relay for the return message, and that message's exact `Message-ID`
was found in the employee's Mailu inbox. One inbound attempt failed on the direct
Docker-to-LAN path and was retried after configuring the SSH return forward.
No experiments or automated test suites were run. The earlier failed capture can
remain in smtp4dev; configuration updates do not resend captured messages.
