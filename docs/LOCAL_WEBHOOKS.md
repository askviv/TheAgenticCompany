# Company webhooks to a local assistant

The company sends native Rocket.Chat outgoing webhook POSTs to a receiver you
already operate. This configuration does not create a receiver, translate its
payload or start an experiment. The URL must be the actual webhook route exposed
by your assistant, not a dashboard page or a made-up path.

Copy [the connection example](../deploy/virtual-time-world/local-connections.example.json)
to a private file and set `chmod 600 /path/to/local-connections.json`. It has two
sections:

| Setting | Meaning |
| --- | --- |
| `webhooks.rocketchat.enabled` | Native integration enabled/disabled state |
| `webhooks.rocketchat.endpoint` | Complete URL reachable from the company, including the receiver's actual path |
| `webhooks.rocketchat.token` | Shared secret sent in Rocket.Chat's native JSON `token` field; the receiver must validate it |
| `webhooks.rocketchat.channels` | Native channel subscriptions, e.g. `["#general", "#finance"]` |
| `tunnel.ssh_key` | Laptop's existing SSH identity for the company host |
| `tunnel.mini` | SSH destination, e.g. `user@company-host` |
| `tunnel.label_prefix` | Local LaunchAgent label prefix |
| `tunnel.webhook_listen_port` | Company host loopback port opened by SSH |
| `tunnel.webhook_host` | Receiver host as reached from the laptop, normally `127.0.0.1` |
| `tunnel.webhook_port` | Receiver's actual local TCP port |

Keep tokens and the filled configuration outside Git. The checked-in example has
an empty token and illustrative receiver path; replace both before applying it.
The native integration is named `TACAssistantEvents`, separate from the existing
`TACExperimentTracking` dashboard hook. Updating this configuration does not
replace the tracker or change its existing channel subscriptions.

## Directly reachable receiver

Set `endpoint` to the receiver's HTTPS URL. No SSH webhook forward is needed;
omit the three `tunnel.webhook_*` values when using the tunnel installer for mail
only. Configure the receiver to accept Rocket.Chat's native outgoing-message
payload and validate the shared token. This is not a combined feed of every ERP
or platform event; ERPNext has its separate native webhook configuration.

## Laptop loopback receiver

When the receiver only listens on laptop localhost, configure the optional SSH
reverse forward. The example uses host port `19160` and local port `8080`; change
them to your own setup. For the timed company VM's QEMU user network, the matching
endpoint is `http://10.0.2.2:19160/<actual-receiver-path>`. `10.0.2.2` is the VM's
existing route to host loopback. Other deployment topologies should use their
own reachable host address. If the local receiver serves HTTPS, use `https` and
its trusted hostname/certificate; SSH does not change the HTTP/TLS protocol.

Install or update the existing Mac tunnel from the private configuration:

```sh
python3 deploy/single-host/smtp4dev-bridge/install-mac.py \
  --config /path/to/private/local-connections.json
```

The installer uses standard launchd and SSH `-R`, binds the host listener to
loopback, and reconnects automatically. It does not expose the receiver on a
public interface or change SSH's `GatewayPorts` policy. It also manages the mail
forwards described in [Local email setup](LOCAL_EMAIL.md): include your existing
SMTP, API and optional reply-forward values in `tunnel` so applying it preserves
the same connections. Explicit CLI options override file values.

## Apply the outgoing integration

For a new deployment, put the `webhooks` object in the private deployment profile.
Dashboard bootstrap provisions the separate native outgoing integration.

For an existing Docker dashboard, apply only the integration using its existing
credentials and the private file on stdin:

```sh
docker exec --interactive tac-experiments \
  python -m experiments.runner --webhook-config /dev/stdin \
  < /path/to/private/local-connections.json
```

Run this on the machine hosting `tac-experiments`; securely transfer the private
configuration there first if necessary. No token is passed on the command line.
For a source-installed dashboard, run `python -m servers.experiments.runner
--webhook-config /path/to/private/local-connections.json` from the source root
using that dashboard's existing Python environment and credential environment.

Reapply after changing endpoint, token, channels or `enabled`. Set `enabled` to
`false` and reapply to stop external deliveries. Removing the file alone does
not remove an already configured native integration. This operation configures
the hook; it does not send a sample webhook or replay existing messages.
