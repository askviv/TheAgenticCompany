#!/bin/bash
set -euo pipefail
umask 027
lineage=/data/mini-web/acme-config/live/company-example-test
target=/data/theagentcompany/https-tls
openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt -untrusted "$lineage/chain.pem" "$lineage/cert.pem" >/dev/null
openssl x509 -in "$lineage/cert.pem" -noout -checkend 0 >/dev/null
openssl x509 -in "$lineage/cert.pem" -noout -checkhost owncloud.company.example.test >/dev/null
[ "$(openssl x509 -in "$lineage/cert.pem" -pubkey -noout | sha256sum)" = "$(openssl pkey -in "$lineage/privkey.pem" -pubout | sha256sum)" ]
install -d -o root -g caddy -m 0750 "$target"
install -o root -g caddy -m 0640 "$lineage/fullchain.pem" "$target/fullchain.pem.next"
install -o root -g caddy -m 0640 "$lineage/privkey.pem" "$target/privkey.pem.next"
mv -f "$target/fullchain.pem.next" "$target/fullchain.pem"
mv -f "$target/privkey.pem.next" "$target/privkey.pem"
caddy validate --config /data/theagentcompany/source/deploy/single-host/https/Caddyfile --adapter caddyfile
# Reload an active proxy; the installer starts it after initial certificate staging.
systemctl try-reload-or-restart tac-https.service
