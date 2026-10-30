# ERPNext runtime and A1 proof

**Status:** deployed; final integration handover
**Deployment:** configured host
**Compose project:** tac-enterprise-erp

## Installed stack

The simulation runs as a separate Docker Compose project on the host. Its persistent data is under /data/theagentcompany/enterprise/erpnext. The only published port is 192.0.2.10:8130 to the ERPNext frontend; MariaDB and both Redis instances have no published host ports. Frappe site routing uses the per-site HTTP Host header (erp-sim-<UUID hex>.localhost). The endpoint is plain HTTP on the private LAN and is not configured as an Internet-facing service.

## Company identity site

The public ERP hostname routes to the fixed owned company-baseline site
`erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost` (UUIDv5 of
`tac:company-baseline:v1`). This site is created by the trusted site-admin
command without creating a simulation run, event, or email. Its golden
baseline is independent from all run-owned sites. The site-admin seed uses
ERPNext's native setup wizard to create exactly one `The Agent Company`
Company with abbreviation `TAC`; it refuses to continue if another Company is
already present. The Company is the default for the Desk and owns the seeded
procurement, inventory, and sales documents.

The site seed imports the canonical roster from
`deploy/single-host/company-identities.json` read-only. It creates linked native
ERPNext System Users and Employee records for all 17 roster entries. Every
employee receives the standard Employee role. The existing coordinator
principals also receive one matching domain role: requester/Purchase User,
warehouse/Stock User, logistics/Delivery User, and manager/Accounts Manager.
The Employee DocType requires date of birth and joining date; the fictional
fixture uses 1990-01-01 and 2026-01-01 rather than inventing personal dates.
The Employee DocType is part of the pinned ERPNext v16 app, so HRMS is not
added to the stack. Existing per-run diagnostic sites remain independent and
are not migrated, merged, or reset by this company baseline.

The public hostname uses Caddy's exact host route. Its previous route target
was `erp-sim-4534e41376275fd2ab3d4da29c90716b.localhost`; that diagnostic site
is preserved and can be restored by reverting only the `Host` value in the
ERP Caddy stanza and reloading `tac-https.service`. Other Caddy sites and the
ERP service bindings are unchanged. The active Caddyfile was backed up to
`/data/theagentcompany/source/deploy/single-host/https/Caddyfile.pre-tac-company-20261029`
before the route change. `caddy validate` succeeded, `tac-https.service` was
reloaded and remained active, and `GET /login` at the public ERP hostname
returned HTTP 200. The backup can be restored with `cp -p` to the active
Caddyfile followed by `systemctl reload tac-https.service`.

| Component | Runtime version or immutable image reference |
|---|---|
| ERPNext image | frappe/erpnext@sha256:74180ed84b3ae245ecad78da94d960034884664caf36d6ea4832cc3bd7152759 (image release v16.53.0) |
| Frappe Framework in image | 16.54.0 (bench version; image reports no source revision) |
| ERPNext in image | 16.53.0 (bench version; image reports no source revision) |
| MariaDB | mariadb@sha256:c4124d1afa875e47f2892edf26b482fa54f796e2f342d3717a04ca091a22a9bd; runtime 11.8.9 |
| Redis | redis@sha256:f4ed7d6732f9a5dcc6b64e2b347860b9f329334bfd4839a672f78cd981a8bba6; runtime 8.6.8 |
| Packaging source reviewed | frappe_docker v4.1.1, commit d84154ae23ab7ae756dfe942e3f24dcd70d1fbda |

The official ERPNext/Frappe image is AMD64-pinned. ERPNext/Frappe/frappe_docker releases, source SHAs and licenses are recorded in the [compatibility audit](erpnext-compatibility.md). The runtime has scheduler disabled by default; backend, frontend, websocket, short/long workers, MariaDB and Redis remain running.

The Compose templates configure resource limits for the application stack
and trusted site-administration jobs. Size them for the target deployment.

## Per-run isolation and reset

The trusted site-admin command derives the Frappe site name from a canonical run UUID and serializes operations with a per-site file lock. A run receives its own native Frappe site/database, credentials, idempotency store and golden Frappe backup. The agent-facing API does not receive the Docker socket or the site-admin/root credentials. Create returns an existing binding only when the run manifest, site and recorded golden-file SHA-256 values agree. Reset invokes Frappe's native restore against that run's golden backup and removes only that run's local idempotency database. It never resets or deletes other runs or company services. Refresh-seed is a trusted admin operation to update one owned run's baseline.

Historical diagnostic sites remain preserved separately from the public company baseline:

| Purpose | Site | Native purchase documents |
|---|---|---|
| Expanded A1 baseline and reset proof | erp-sim-2a2d68c5f6b64c6c9ec45f79d529acb7.localhost | PO PUR-ORD-2026-00001, receipt MAT-PRE-2026-00001, draft invoice ACC-PINV-2026-00001 |
| Prepared S01 run | erp-sim-159ce6579a6d5edb93aa82f67ec4e6d3.localhost | PO PUR-ORD-2026-00001, receipt MAT-PRE-2026-00001, draft invoice ACC-PINV-2026-00001 |
| Clean contract-proof run | erp-sim-4534e41376275fd2ab3d4da29c90716b.localhost | PO PUR-ORD-2026-00001, receipt MAT-PRE-2026-00001, draft invoice ACC-PINV-2026-00001 |

Each site also contains the same fictional supplier, company, component, warehouse, customer and submitted customer Sales Order SAL-ORD-2026-00001. The S01 site was prepared under its own UUID; no goods receipt, invoice approval or payment operation has been made on it.

## Historical A1 native-state proof

The native seed contains a submitted 10,000-unit Purchase Order line, one submitted 8,000-unit Purchase Receipt linked to that same PO row, and a draft 10,000-unit Purchase Invoice linked to the PO row. The receipt changes ERPNext stock to 8,000. No Payment Entry exists. A1's expected billed-but-unreceived quantity is therefore 2,000.

The probe uses the authenticated ERPNext REST API and the adapter's evaluator-side linked-row assessment. It also reads the native customer/Sales Order and checks submitted Sales Invoice returns and refund Payment Entries linked through that order. Results on the expanded A1 baseline:

- PO ordered: 10000; submitted receipt: 8000; supplier invoice billed: 10000.
- Adapter's unreceived billed quantity: 2000; native stock balance: 8000.
- Invoice remains docstatus 0; the review request moves its native workflow state to Review Requested, still docstatus 0.
- Submitted purchase payments: 0; submitted sales refunds/credit notes: 0.
- Customer and Sales Order reads succeed; Sales Order is submitted.

Before and after native reset of the expanded golden baseline, the probe passed with the same pre-review fingerprint:

    994f75e82e365b2c9c9db85cd00a99611c7e6ae24616d66e660ee0d6de18cb6f

The probe does not submit the Purchase Invoice or create a payment. A separate test of the original M1 seed before adding the customer/Sales Order also passed with fingerprint fc196c81bc6e222e84459a13e757b02915995ad9d3153b3f61ba379f061510b4; its reset reproduced that fingerprint exactly.

## Adapter and authorization boundary

servers/simulation/erpnext.py reads allowlisted native fields for Purchase Orders, Receipts, Invoices, Suppliers, Customers, Sales Orders, stock and payment/refund state. Quantities and amounts are returned as Decimal. A1 assessment only counts submitted Purchase Receipt parent documents and item rows linked to the invoice's PO item; it does not query child DocTypes directly because Frappe denies direct child-table reads to the integration role.

Invoice review invokes Frappe's native workflow action and requires a draft/pre-submit invoice. The integration role can request review but cannot submit invoices or make payments. Approved goods receipt creation is a separate trusted engine call: it requires an immutable engine VerifiedERPApproval bound to run UUID, action, PO, exact Decimal quantity, PO modified version, policy version and UTC-derived posting date/time. It rechecks that the quantity equals the PO row's exact outstanding quantity and preserves the source warehouse. A separate API identity has only the native receipt permissions needed for that call. A SHA-256 operation marker is stored on the native receipt, and a per-run SQLite ledger provides one-time approval consumption/idempotent replay. The agent HTTP payload cannot supply a boolean approval or construct that trusted grant.

The adapter also implements `commit_expedited_shipping` for a manager-authorized supplier quote. It creates and submits a distinct Purchase Order with the seeded non-stock `EXP-SHIP-SVC` item at the exact GBP quote amount, tied by a unique native custom approval-marker field and the standard `terms` field to the approved quote, shipment and original component PO. The original component PO is checked against its approved modified version and left unchanged. A dedicated shipping poster identity has only Purchase Order create/write/submit plus the read permissions needed by native validation. The coordinator credential loader and engine action route pass the separate shipping key and verified approval. The adapter has local scripted-transport coverage for native request, readback fields and idempotent recovery. The first server attempt exposed that ERPNext Purchase Order has no native `remarks` field; it failed at the marker lookup with HTTP 417 before creating a service PO. The adapter now uses the custom field, and the native migration path creates it with Frappe's `create_custom_fields` and enforces uniqueness. On site `erp-sim-71587bb54ee95d7c85f477f6860107eb.localhost`, the setup-only migration verified the unique field and set `System Settings.time_zone` to UTC through the native singleton document lifecycle. Native `get_system_timezone()` now returns UTC. The migration filled only missing mandatory System Settings locale fields (`language=en`, `date_format=yyyy-mm-dd`, `time_format=HH:mm:ss`, `number_format=#,###.##`) required for a valid singleton save. Readback confirmed the PO remains submitted, the A1 receipt remains submitted, and the invoice remains draft; no shipping PO was created. The golden database still matches its recorded checksum. The diagnostic run was subsequently stopped; executing a financial workflow is excluded from the final integration handover.

The prepared S01 site's owned golden baseline was refreshed to include the non-stock shipping service item and its dedicated poster credentials. A native read confirmed the item exists, and the private credential file remains mode `0600`; no service Purchase Order has been created on that run. The earlier diagnostic runs are stopped and preserved. The clean contract-proof site was created from the reviewed seed after the native submit-payload fix; its baseline has a unique approval-marker field, UTC System Settings, receipt and shipping writer roles, private credentials at mode `0600`, and the original submitted 8,000/10,000-unit A1 purchase state. No shipping PO, goods receipt, invoice approval or payment action has been made on the clean site.

The native approved-receipt path has local regression coverage. It has not been run against the prepared S01 site; no receipt action is performed until the engine's authenticated approval flow calls it. ERPNext itself rejects future Purchase Receipt posting dates, so the simulation anchor is 2026-10-06T09:00:00Z and Day 14 is 2026-10-19, within the host's current date. The native seed dates are PO transaction 2026-10-01, planned delivery 2026-10-06, and A1 invoice/receipt posting 2026-10-06. Frappe returned the existing A1 receipt's `posting_time` as `1:43:27.80816`; the adapter now normalizes this provider value to canonical `01:43:27.808160` at its boundary and compares granted/readback times semantically. This DTO contract avoids provider-specific formatting in the engine.

## Mail and verification

The ERPNext site uses the existing central Mailu SMTP account on port 2587 with TLS. Site setup validates the account through Frappe's native Email Account validation; no email was sent during deployment or probing. No new mail stack was added.

Local checks run after the adapter and runtime changes:

    python3 -m ruff check servers/simulation/erpnext.py servers/simulation/tests/test_erpnext.py deploy/enterprise-simulation/erpnext/admin
    python3 -m py_compile servers/simulation/erpnext.py deploy/enterprise-simulation/erpnext/admin/*.py
    python3 -m unittest servers.simulation.tests.test_erpnext -v

The eleven adapter tests passed, covering A1 row-link assessment, pre-submit workflow/idempotency, error redaction/cause preservation, UUID-scoped site administration, verified goods receipt binding/replay, Frappe time normalization/error preservation, the shipping-order scripted transport, and native Sales Invoice return/Payment Entry refund interpretation. Adapter tests use a scripted HTTP transport; the runtime A1 and reset evidence above is from the isolated ERPNext instance.
