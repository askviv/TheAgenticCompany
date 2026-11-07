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

## Image packaging

`deploy/enterprise-simulation/erpnext/business-apps/build.sh` uses the unmodified
Frappe Docker Containerfile from the recorded upstream revision and the native
`apps.json` list. ERPNext and Frappe retain their existing versions. Helpdesk
requires Frappe Telephony; this dependency is included without configuring a phone
provider. Telephony has no published version tag and uses its upstream develop
branch. The remaining applications use explicit release tags. This packages apps
in the normal Frappe image; it introduces no custom agent runtime or app patches.

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
preserves the rendered Compose settings, changes only app service images, stops
app workers during native schema installation, installs apps on the existing
company site, provisions directory projections, and starts the services again.
It never creates a new company or restores a fixture snapshot. Native application
errors stop the upgrade and remain visible; resolve them and rerun the command.

Salary structures, loan products, support mail accounts and CRM integration
settings are configured through the native applications. No external phone,
lending, payment, tax or model provider is activated by this setup.
