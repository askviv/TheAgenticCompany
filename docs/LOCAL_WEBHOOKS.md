# Company webhooks to a local assistant

The company sends native Rocket.Chat outgoing webhook POSTs to a receiver you
already operate. This configuration does not create a receiver, translate its
payload or start an experiment. The URL must be the actual webhook route exposed
by your assistant, not a dashboard page or a made-up path.

Copy [the connection example](../deploy/virtual-time-world/local-connections.example.json)
to a private file and set `chmod 600 /path/to/local-connections.json`. It contains the following settings:

| Setting | Meaning |
| --- | --- |
| `webhooks.rocketchat.enabled` | Native integration enabled/disabled state |
| `webhooks.rocketchat.endpoint` | Complete URL reachable from the company, including the receiver's actual path |
| `webhooks.rocketchat.token` | Shared secret sent in Rocket.Chat's native JSON `token` field; the receiver must validate it |
| `webhooks.rocketchat.channels` | Native channel subscriptions, e.g. `["#general", "#finance"]` |

Keep tokens and the filled configuration outside Git. The checked-in example has
an empty token and illustrative receiver path; replace both before applying it.
The native integration is named `TACAssistantEvents`, separate from the existing
`TACExperimentTracking` dashboard hook. Updating this configuration does not
replace the tracker or change its existing channel subscriptions.

## Tailscale connection to a local receiver

Use the standard Tailscale clients on the company host and laptop, connected to
one tailnet. No project forwarding process or SSH webhook tunnel is required.

On the laptop, publish the existing local receiver privately using native Serve:

```sh
tailscale serve --bg --https=8443 http://127.0.0.1:ACTUAL_RECEIVER_PORT
tailscale serve status
```

Replace `ACTUAL_RECEIVER_PORT` with the running receiver's port. Put the returned
HTTPS hostname and port, followed by the receiver's actual webhook path, in
`webhooks.rocketchat.endpoint`. The company VM must be able to reach that address
through its host network; tailnet access rules must permit the company host.
Use a separate Serve port if an existing service already occupies it. Do not
use Funnel: this receiver should be private to the tailnet. The native Tailscale
client manages the connection; no Python installer is needed for webhooks.

The receiver must accept Rocket.Chat's native outgoing-message payload and
validate its JSON token. A generic endpoint requiring signed adapter envelopes
is not interchangeable with this receiver. This is not a combined feed of every
ERP or platform event; ERPNext has separate native webhook configuration.

For a directly reachable HTTPS receiver, configure its URL together with the
nonempty token, channel list and explicit enabled state.
The existing SSH mail setup is independent and remains unchanged.

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
