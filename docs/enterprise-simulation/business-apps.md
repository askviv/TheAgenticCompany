# Payroll, lending, CRM and Helpdesk

The company ERPNext site includes Frappe HR, Frappe Lending, Frappe CRM and Frappe
Helpdesk. ERPNext Accounting remains the accounting system. The existing
spreadsheet payroll experiment remains available and unchanged. Installing the
apps does not process payroll, issue loans, run experiments or enable model calls.

All apps share native Frappe User identities on the existing company site.
`deploy/single-host/company-identities.json` is the authoritative employee and
department directory. `erpRoles` retains ERP permissions; `frappeAppRoles` maps
those same users to native application roles. Provisioning links Employee
records to the same users, sets native departments, and projects department
membership into native Helpdesk teams. No second password or employee directory
is created. Directory changes are applied by rerunning native app provisioning.
Existing unrelated accounts, roles and company records are preserved.

Applications use the ERPNext origin; no new domain or database is required:

- Payroll and employee services: `/hrms` (native HR administration is in Desk).
- Lending: `/app/lending`.
- CRM: `/crm`.
- Helpdesk: `/helpdesk`.
- Accounting: the existing ERPNext Desk accounting workspace.

The dashboard's Company tools links use the deployment's
`COMPANY_TOOLS_URLS_FILE` JSON mapping. It includes `payroll`, `lending`, `crm`
and `helpdesk` alongside the existing tools. Configure their full URLs there;
the dashboard does not hard-code a deployment domain. Fresh bootstrap derives
these four URLs from the configured ERP domain. See
`servers/experiments/company-tools-urls.example.json` for the complete mapping.
The CRM dashboard link uses Frappe's native login redirect because its app page
requires a signed-in user with CRM access.

## Optional passkey login

The image also includes the third-party `Andrometiq/frappe-passkeys` app, pinned
to `v16.0.7` for Frappe 16. Password login remains available. Provisioning applies
the native fields in `admin/passkey-settings.json`: passwordless passkey login is
enabled, second-factor mode is off, and no users are required or nudged to enrol.
The RP ID and HTTPS origin come from the configured ERPNext public URL, not from
request headers or a hard-coded deployment domain.

For an existing company, `COMPANY_TOOLS_URLS_FILE` selects the host-side URL
mapping read by `business-apps/install.sh` (default:
`/data/theagentcompany/enterprise/secrets/company-tools-urls.json`). Fresh
bootstrap uses the deployment profile's ERP domain. The native settings validator
checks the resulting passkey configuration before enabling it.

Users can sign in normally and open `/passkeys` to register their own passkey.
Then they can use the passkey option on `/login`. System Managers can inspect the
configuration at `/desk/passkey-settings`. No passkey is registered by deployment,
and credentials remain in the existing Frappe site rather than a second directory.

## Image packaging

`deploy/enterprise-simulation/erpnext/business-apps/build.sh` uses the unmodified
Frappe Docker Containerfile from the recorded upstream revision and the native
`apps.json` list. ERPNext and Frappe retain their existing versions. Helpdesk
requires Frappe Telephony; this dependency is included without configuring a phone
provider. Telephony has no published version tag and uses its upstream develop
branch. The remaining applications use explicit release tags. This packages apps
in the normal Frappe image; it introduces no custom agent runtime or app patches.
The upstream builder's `CACHE_BUST` argument uses the app manifest's SHA-256 because
Docker does not include secret-file content in cache keys. The image records the
manifest digest, upstream builder revision and Frappe branch as labels. A local
image is reused only when all three match; a missing label or changed manifest
causes a native rebuild, preserving unrelated BuildKit layers. The default image
tag comes from `erpnext/compose.yml`; `ERP_IMAGE` overrides it consistently for the
build and installer.
Docker's standard container build driver keeps temporary build storage in the
Docker data directory. `ERP_IMAGE_OUTPUT` can select a Docker image archive on a
data drive (for example `type=docker,dest=/data/build/company-apps.tar`) when the
host system disk is tight. Load that image into the VM with `docker image load`
and remove the temporary archive afterward. The default output loads the image
into the local Docker daemon. The build container is stopped after a successful
build; its cache contains application packages, never company records.

Fresh VM bootstrap builds this image before the accelerated clock is activated,
then installs the apps and applies the directory configuration to the shared
company. Existing run-specific ERP sites are not reset or changed.

For an already accelerated VM, build on a real-time Linux Docker host and stream
the image into the guest with `docker image save ... | ssh ... sudo docker image
load`. Repository metadata expiry checks remain enabled. No company state or
persistent import archive is copied.

## Existing company upgrade

Deploy the updated repository files, including the directory configuration and
`admin` modules, to the locations mounted by the ERP Compose project. Preserve
instance-specific identity settings when updating its private directory file. The
upgrade initializes missing application-role bindings from the repository directory
by employee ID; existing configured bindings and employee identities are retained.
Load the native app image first if the guest clock has already advanced, then run
inside the company VM:

```sh
sudo bash /data/theagentcompany/enterprise/source/deploy/enterprise-simulation/erpnext/business-apps/install.sh
```

`ERP_COMPOSE` and `SIMULATION_CONFIG` override deployment paths. The command
preserves other rendered Compose settings, updates app service images, and sets
the `site-admin` service's `TAC_ERP_PUBLIC_URL` from the configured `erpnext` URL in
`COMPANY_TOOLS_URLS_FILE`. The directory and rendered Compose file are written
atomically. App workers stop during native schema installation; apps are installed
on the existing company site, directory projections are provisioned, and services
start again. It never creates a new company or restores a fixture snapshot.

If stopping, native installation or the final startup fails, the exit handler
attempts to bring back the app services that were running before the upgrade.
The original failure exit code and native error output are retained; a failed
recovery is reported separately. This does not roll back native database migrations.
Resolve the reported native problem and rerun the installer if recovery cannot
start the services. A host power loss cannot run a shell exit handler.

Salary structures, loan products, support mail accounts and CRM integration
settings are configured through the native applications. No external phone,
lending, payment, tax or model provider is activated by this setup.

Record the deployed native image identity with `docker image inspect --format '{{.Id}}' "$ERP_IMAGE"` (use the configured Compose backend image when `ERP_IMAGE` is unset). The local build must exist on the target host before Compose starts; bootstrap builds it before rendering the stack. App branches/tags remain upstream references: Bench’s native apps manifest does not support commit checkout, so Telephony’s `develop` reference is not an immutable source pin.
