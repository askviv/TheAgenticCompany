#!/bin/bash
set -euo pipefail
lineage=/data/mini-web/acme-config/live/company-example-test
if openssl x509 -in "$lineage/cert.pem" -noout -checkend 2592000; then
    /data/theagentcompany/source/deploy/single-host/https/refresh-certificate.sh
else
    certbot renew --non-interactive --cert-name company-example-test \
      --config-dir /data/mini-web/acme-config --work-dir /data/mini-web/acme-work --logs-dir /data/mini-web/acme-logs \
      --manual-auth-hook /data/mini-web/acme-manual/auth.sh \
      --manual-cleanup-hook /data/mini-web/acme-manual/cleanup.sh \
      --deploy-hook /data/theagentcompany/source/deploy/single-host/https/refresh-certificate.sh
    /data/theagentcompany/source/deploy/single-host/https/refresh-certificate.sh
fi
