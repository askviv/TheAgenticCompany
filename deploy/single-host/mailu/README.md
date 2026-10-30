# Private Mailu service

This is a shared mail service for the synthetic `@example.com` employee accounts. It does not create per-experiment servers or volumes. Images are pinned to Mailu `2024.06.58`; ClamAV, attachment search, and fetchmail are disabled. Rspamd remains enabled.

## Before deployment

1. Confirm the host has enough remaining memory and disk capacity for Mailu alongside existing services. Mailu's published minimum without ClamAV is 1 GB RAM plus 1 GB swap; that is not a capacity guarantee for this host.
2. Confirm `172.22.0.0/24` remains unused on the host and on reachable Docker/Kubernetes networks. the host's inspected active Docker networks currently use `172.17.0.0/16` through `172.21.0.0/16`; change the Compose subnet and the matching `.env` subnet together if the route inventory changes.
3. Add private DNS for `mail.company.example.test` pointing to `192.0.2.10`. the host already has a certificate whose SAN covers this name; Compose mounts `/data/theagentcompany/https-tls/` read-only into Mailu and uses the configured `fullchain.pem` and `privkey.pem` filenames. Caddy can use the same host certificate independently. After certificate renewal, reload Mailu's front container so its SMTP/IMAP TLS listeners use the updated files.
4. The deployed exact recipient `agent@agent.example.test` routes through `172.22.0.1:19126` to the existing local smtp4dev service. See `../smtp4dev-bridge/README.md`. Only addresses explicitly listed in `overrides/postfix/recipient-transport.map` use this route; company employee addresses remain local mailboxes.
5. Ensure the host firewall permits SMTP/IMAP only from the experiment runner and intended users. The Compose bindings are limited to the host's LAN address; Caddy's Mailu HTTP upstream is loopback-only on port 8112.

The bundled Unbound resolver was tested but could not validate the current root-key transition on this Mini. Mailu containers therefore use Quad9 and Cloudflare DNS directly; both returned DNSSEC authenticated-data responses during the deployment check. This exposes DNS queries to those resolvers, but Postfix's default and relay transports reject public email delivery. Do not change the DNS resolver setting to a non-validating server or disable DNSSEC checks.

## Configuration and access

Create `/data/theagentcompany/mailu/mailu.env` from `.env.example`, replace `SECRET_KEY` and `API_TOKEN` with private random values, and restrict the file to the deployment account. The secret file is outside version control. The API token grants broad Mailu administration access. Do not place it in browser-visible settings or logs.

The webmail and admin UI are served by the front container on `127.0.0.1:8112`. The deployed Caddy site for `mail.company.example.test` proxies HTTPS requests to that loopback endpoint. Employee webmail is available at `/webmail/`; administration and the API are under `/admin/` and `/api/`.

Local protocols use `mail.company.example.test:2587` for authenticated SMTP submission with TLS, `mail.company.example.test:2993` for IMAP over TLS, and `192.0.2.10:2525` for SMTP ingress. Port 2525 is not public port 25 and must remain LAN/firewall restricted. The server's default Postfix delivery routes are configured to reject external destinations; do not add broad relay networks or a global relay host.

Rspamd's mounted `overrides/rspamd/milter_headers.conf` sets `add_smtp_user = true` for its existing `Authentication-Results` routine. Rspamd removes upstream Authentication-Results headers before adding its own; the simulation accepts an authenticated sender only from one result with the configured Mailu `authserv-id`, `auth=pass`, and an `smtp.auth` username that maps to the configured actor and matches the message's From address. It also checks the exact thread and mailbox recipient. The IMAP observer fetches headers before the body and reads body content only after these checks. This uses Rspamd's standard milter header routine documented at [Rspamd milter headers](https://docs.rspamd.com/modules/milter_headers/). A controlled internal delivery and a spoofed upstream Authentication-Results header were verified against existing employee mailboxes; only exact headers were read back.

The exact-recipient Postfix map uses Mailu's supported Postfix override mechanism. Mailu compiles the mounted `.map` file for Postfix and keeps its built-in transport lookups. The deployed entry is `agent@agent.example.test smtp:[172.22.0.1]:19126`. Review and verify any additional recipient before adding it. This is a recipient-specific transport route, separate from mailbox forwarding.

For authenticated Viv replies, `viv-sender-login.map` maps only `agent@agent.example.test` to `viv_reply_relay@example.com`, layered ahead of Mailu's normal sender-login map. This is separate from the inbound recipient route above. The relay account is SMTP-only, non-administrative, and has `allow_spoofing=false`; no domain-wide sender permission is granted.

## Provision seeded employee mailboxes

Copy `deploy/single-host/company-identities.json` to `/data/theagentcompany/mailu/company-identities.json`, then run `python3 /data/theagentcompany/mailu/provision.py`. The helper uses Mailu's API, creates only missing accounts, and preserves existing passwords on reruns. It never deletes or resets Mailu users. It also provisions `viv_reply_relay@example.com` as a non-spoofing SMTP-only service account and stores its password in the existing mode-0600 `service-credentials.json`. An existing relay account without a managed password, or with spoofing/admin rights, requires operator recovery. Protect and back up the employee and service credential files. The API token remains only in `mailu.env`.

## Validate locally

After preparing `/data/theagentcompany/mailu/mailu.env` and ensuring the host certificate is ready, inspect the rendered configuration before starting anything:

```sh
docker compose config --quiet
```

Configuration validation does not start services. The deployed service is already running; use its existing private environment and persistent directories when restarting it.
