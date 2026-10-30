# Local Viv mail bridge

The shared smtp4dev UI belongs to the local Kubernetes platform and is available at http://127.0.0.1:9167. This bridge uses the same `app.kubernetes.io/name=smtp4dev` service in the explicit `docker-desktop` context. It discovers its namespace after restarts; it never edits or replaces that service. The native reply relay needs only the additional Mailu hostname mapping described below.

the host cannot initiate a TCP connection to the Mac's LAN listener. Native SSH reverse forwards keep SMTP and the receipt API reachable without opening public ports or changing sshd's `GatewayPorts=no` policy:

- Mac loopback 19126 → Kubernetes smtp4dev port 25.
- Mac loopback 19158 → Kubernetes smtp4dev port 80.
- Mini loopback 19125/19157 → the two Mac listeners over SSH.
- Mini `systemd-socket-proxyd` exposes only SMTP on Mailu's Docker bridge `172.22.0.1:19126` → Mini loopback 19125. The API remains on Mini loopback.

Install the Mac user LaunchAgents using `python3 install-mac.py --ssh-key /path/to/existing/key`. The existing known-host verification remains enabled. The installed copy is outside Documents to avoid requiring macOS Full Disk Access. Logs contain connection lifecycle messages, not mail bodies.

Install `tac-smtp4dev.socket` and `tac-smtp4dev.service` under `/etc/systemd/system/` on the host, run `systemctl daemon-reload`, and enable/start only `tac-smtp4dev.socket`. Mailu's Docker network must exist first. The Mailu exact-recipient transport map sends `agent@agent.example.test` to this socket; other external recipients are rejected.

For replies, apply `mail-host-alias.json` as a strategic merge patch to the shared smtp4dev Deployment in its discovered namespace, preserving all existing settings. This supplies DNS for `mail.company.example.test` without changing cluster DNS. Provision Mailu first so `/data/theagentcompany/mailu/service-credentials.json` contains the managed `viv_reply_relay` credential, then install and start the SSH tunnel. Configure the existing smtp4dev service from the host through the reverse API tunnel:

```sh
ssh -F /dev/null -i /path/to/existing/key company@192.0.2.10 \
  'python3 /data/theagentcompany/enterprise/source/deploy/single-host/smtp4dev-bridge/configure_reply.py --base-url http://127.0.0.1:19157'
```

The script reads the private credential file on the host and updates smtp4dev's existing native relay settings over the loopback tunnel. It configures STARTTLS on authenticated Mailu submission port 2587 and leaves the recipient policy and other smtp4dev settings intact. The password is not printed or placed in command arguments; smtp4dev stores it in its existing persistent settings volume. The privileged Server API may return that password on readback, so do not dump, log, or expose Server API responses. The configurator performs bounded read-only checks after one update and verifies the password if it is returned. Mailu's exact sender-login map permits that relay login to submit only `agent@agent.example.test`. The automatic expression continues forwarding only `@example.com` recipients, so incoming mail addressed to Viv stays in smtp4dev rather than looping back.

Mailu skips only greylisting for the specific Mac gateway IP `192.168.0.118/32`; sender-domain checks, other antispam checks, and recipient/external-delivery restrictions stay active. This is required because smtp4dev is a simulator rather than a durable retrying MTA. Change the scoped IP rule if the Mac's LAN address changes.

To stop this bridge, boot out both `org.example.tac-smtp4dev-*` user LaunchAgents and disable/stop `tac-smtp4dev.socket` and `tac-smtp4dev.service` on the host. To remove automatic replies, use smtp4dev's settings to clear the company relay host and expression. Do not delete messages, Mailu volumes, or the shared smtp4dev service. Restore the host-alias field only if it still matches this overlay; preserve other aliases added subsequently.
