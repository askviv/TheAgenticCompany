# ERPNext / Frappe compatibility audit

**Reviewed:** 2026-10-29 (UTC)
**Scope:** ERPNext purchase, receipt, invoice and payment contracts; supported Frappe APIs and workflows; seed/reset characteristics; official container layout; fit with this simulation's A1 invoice mismatch and current company services.

## Decision

ERPNext is a fit as the authoritative procurement and payable ledger. Keep it as a separate service behind `erpnext-adapter`; the coordinator should call Frappe's permission-checked HTTP API and retain canonical IDs/event history outside ERPNext. Use ERPNext's own linked documents and workflow actions for approval state. Do not insert records into MariaDB or infer business state from scraped UI values.

For a new deployment, pin the **v16** line together: ERPNext `v16.53.0` (`9cd11b19575bfc34f092a24f6c8b368c4dd69481`), Frappe `v16.53.0` (`e8d41dd0061a32ee781caefe5d0b2d2bb7c15362`), and official `frappe_docker` `v4.1.1` (`d84154ae23ab7ae756dfe942e3f24dcd70d1fbda`). ERPNext's tag declares `frappe >=16.50.0,<17.0.0`; the Frappe pin satisfies it. Pin container image tags/digests and the Compose repo revision independently in the eventual deployment lock record. These are audited compatible source pins, not a claim that a deployment has been run.

The checked-out `frappe_docker` production Compose base declares `platform: linux/amd64` for backend services. Its official deployment docs discuss Apple Silicon/ARM64 and published multi-architecture images, but its current base Compose file is amd64-pinned. The Mac's actual CPU architecture and the selected image manifest must therefore be confirmed before choosing native ARM vs amd64 emulation. Avoid the `pwd.yml` demo for this integration: upstream labels it disposable, short-lived and unsuitable for installing custom apps.

## Reviewed upstream versions and licenses

| Project | Reviewed release / commit | License | Findings |
|---|---|---|---|
| [ERPNext](https://github.com/frappe/erpnext/tree/v16.53.0) | `v16.53.0` / `9cd11b19575bfc34f092a24f6c8b368c4dd69481` | GNU GPL-3.0 (repository `license.txt`) | Purchase documents and controllers are native; source pin requires Frappe 16.50 through 16.x. Requires Python >=3.14 per tagged `pyproject.toml`. |
| [Frappe Framework](https://github.com/frappe/frappe/tree/v16.53.0) | `v16.53.0` / `e8d41dd0061a32ee781caefe5d0b2d2bb7c15362` | GNU GPL-3.0 | Provides DocType REST/RPC APIs, document permission checks, workflow engine and background queues. |
| [frappe_docker](https://github.com/frappe/frappe_docker/tree/v4.1.1) | `v4.1.1` / `d84154ae23ab7ae756dfe942e3f24dcd70d1fbda` | MIT | Official container/Compose packaging. Base layout includes backend, frontend, websocket, queue workers and scheduler; database and Redis are Compose overrides. |

**Version caveat:** The published source tags are patch releases; ERPNext and Frappe patch numbers need not be identical. The actual constraint to enforce is ERPNext's declared Frappe range. A tested deployment lock should additionally capture the final Docker image digests, MariaDB/Redis tags and site migration state. No deployment has been performed in this audit.

## Supported transaction and API contracts

Frappe exposes DocTypes through its supported REST API and whitelisted methods through RPC. In v15+ the `/api/v1/` resource prefix and `/api/v2/` document API are available. REST supports document create/read/update/delete; v2 also exposes document methods including `submit`, while RPC calls `/api/method/<whitelisted dotted path>`. Requests are evaluated as the authenticated Frappe user; the API key/secret token should belong to a dedicated integration user with minimum roles and be injected from secret storage. Never use `Administrator` for the adapter. See [REST API docs](https://docs.frappe.io/framework/user/en/guides/integration/rest_api) and [authentication docs](https://docs.frappe.io/framework/user/en/api/rest).

The standard purchase chain is `Purchase Order` → `Purchase Receipt` → `Purchase Invoice` → `Payment Entry`. PO submission records a commitment, not inventory or a GL entry. Submitting a Purchase Receipt updates stock (and perpetual-inventory ledgers); submitting a Purchase Invoice posts payable/accounting entries; a Payment Entry settles the payable. Purchase Invoice's `update_stock=1` is a distinct valid short path that can also alter stock, so the adapter must preserve/inspect it and must never create a receipt and an update-stock invoice for the same goods. ERPNext allows partial receipts/invoices and tracks progress against source rows. Sources: [Buying module](https://docs.frappe.io/erpnext/buying), [procurement cycle](https://docs.frappe.io/erpnext/procurement-cycle-overview), and [purchase-cycle ledger impact](https://docs.frappe.io/erpnext/purchase-cycle-ledger-impact).

For procurement, read these real fields (document `name` is ERPNext's key):

| Record | Contract fields relevant to A1 |
|---|---|
| Purchase Order | Header `supplier`, `company`, `docstatus`; child `items[].item_code`, `qty`, `name` (row key), `received_qty`, `billed_amt`; header progress `per_received`, `per_billed`. The PO row key is the `purchase_order_item` foreign key used by downstream lines. |
| Purchase Receipt | Header `docstatus`, `supplier`; child `items[].item_code`, `purchase_order`, `purchase_order_item`, `received_qty`, `qty` (label: accepted quantity), `rejected_qty`, `warehouse`. `received_qty` is the source quantity received; `qty` is accepted quantity. Do not conflate the two. |
| Purchase Invoice | Header `supplier`, `bill_no` (supplier invoice reference), `posting_date`, `docstatus`, `grand_total`, `outstanding_amount`, `paid_amount`, `update_stock`; child `items[].item_code`, `qty` (billed quantity), `purchase_order`, `po_detail` (PO item row), `purchase_receipt`, `pr_detail` (receipt item row), `received_qty`. |
| Payment Entry | Header `payment_type`, `party_type`, `party`, `paid_amount`, `reference_no`, `reference_date`, `docstatus`; child `references[]` identifies linked invoice via `reference_doctype`, `reference_name`, `allocated_amount`. Split/combined settlement should be evaluated from these allocations and invoice outstanding balance, not a custom payment reference parser. |

These names were checked against tagged ERPNext v16.53.0 DocType JSON and controllers. ERPNext `docstatus` is the lifecycle (0 draft, 1 submitted, 2 cancelled); the display/status field is not a substitute. `submitted` Purchase Invoices create accounting effect. Read-only agents can report submitted records, while mutation tools should create drafts only when needed and not submit or create `Payment Entry` without authorized approval.

### Workflow and approval behavior

Frappe Workflows are configured per DocType with states, document status, transition roles and optional conditions. They override the regular Save/Submit path for that DocType; Workflow Actions surface eligible transitions. A transition to `docstatus=1` submits a transaction, which on Purchase Invoice posts payable entries. Therefore an invoice-review request must be represented by a **pre-submit** workflow state/action at `docstatus=0`, or by an explicitly configured review record outside the invoice. The integration adapter must invoke the supported workflow action/document method as the integration user and allow only the intended transition; it must not treat a review request as invoice approval or payment approval. Apply a separate constrained workflow to `Payment Entry` if payment release is in scope. See [Workflows](https://docs.frappe.io/erpnext/workflows) and [Workflow Actions](https://docs.frappe.io/erpnext/workflow-actions).

`frappe_docker` v4.1.1 is compatible with adding a custom Frappe app through its documented custom-image/build flow. A small adapter-facing app is justified only if API permissions or transition behavior cannot be expressed with standard roles/workflows. Its job would be narrowly scoped whitelisted methods/fixtures for the approved tool surface—not a parallel invoice/receipt model. Keep that app in the container image; do not mutate running site code ad hoc.

## A1: exact ERP representation and adapter boundary

**Scenario:** PO ordered 10,000 components; receipts total 8,000; supplier invoice claims 10,000. The agent must identify the 2,000-unit discrepancy, link the right records, request invoice review, and create neither another goods receipt nor an unauthorized payment.

Seed the submitted PO with one 10,000-quantity item row. Seed a submitted Purchase Receipt with the same `item_code`, `purchase_order`, and `purchase_order_item` row link, and 8,000 `received_qty` (with the accepted quantity represented in `qty`; rejected quantity is separate). Represent the supplier's 10,000-unit claim as a Purchase Invoice draft with `bill_no`, item `qty=10000`, and its PO source row reference (`purchase_order`, `po_detail`). If the test also models a receipt linkage, link only actual receipt item rows and preserve their quantity; do not fabricate an 10,000-unit receipt link. Keep `update_stock=0`. `docstatus=0` lets the benchmark hold an invoice claim for review without recognizing the payable. Validate mismatch as invoice billed quantity minus accepted/received quantity, grouped by the linked PO item row; expected A1 delta is 2,000.

The existing The Agent Company `finance-invoice-matching` task is an Excel/PDF matching task, not an ERP contract: its evaluator treats invoice IDs as seven-character text references and computes payment totals from spreadsheets. It does not establish ERPNext's document fields, supplier reference uniqueness, or line-level links. A1 should therefore be implemented as ERP-backed linked documents and scored against ERPNext durable state, not by adapting those spreadsheet assumptions.

**Adapter responsibilities required beyond native ERPNext:**

1. Expose the brief's bounded operations (`get_purchase_order`, `get_purchase_receipt`, `get_purchase_invoice`, `get_supplier`, `get_stock_balance`, `create_goods_receipt`, `request_invoice_review`, `read_payment_status`) over a stable simulation API. Map canonical scenario IDs to ERPNext `name` values and return only fields permitted for that operation.
2. Implement A1's comparison across linked rows: PO `qty`; all submitted Purchase Receipt child `received_qty` and accepted `qty` minus rejected quantity according to the chosen policy; draft/submitted invoice billed `qty`; `docstatus`; invoice-to-PO `po_detail` and receipt-to-PO `purchase_order_item`. ERPNext stores these records but does not provide the benchmark's exact `2,000 missing` finding or scenario pass/fail judgment.
3. `request_invoice_review` should apply a pre-submit invoice workflow transition and record/retrieve its resulting state. A note/comment alone is not an approval state. Do not call `submit` as part of this operation.
4. `create_goods_receipt` needs coordinator-authorized arrival evidence; draft creation is consequentially narrower than submission but still must link the exact PO item, quantity and warehouse. The coordinator owns simulated shipment truth; it may authorize an actual receipt only after delivery arrival. Do not expose create/submission to an agent for A1.
5. Add idempotency/correlation mapping in the coordinator for repeated calls and audit events. ERPNext document APIs do not know the simulation's scenario run ID, hidden truth, or reset/replay event stream by default.

Existing native ERPNext functionality to reuse: stock ledger and balances; linked purchase document generation; received/billed quantity rollups; supplier ledger/outstanding balances; built-in role permission checks; workflows and workflow actions; standard REST/RPC and document submit/cancel lifecycle. No new ERP validation utility, ledger, or payment/reference parser is needed.

## Deterministic seed and reset feasibility

**Feasible with an adapter/runner, not turnkey.** ERPNext provides site creation, fixtures/app installation, document APIs, and database backup/restore. It does not supply a benchmark-level “reset this scenario and replay the same external events” operation. Build a per-run site or restore a known clean site backup, install the pinned app/fixtures, then seed masters and transactions in dependency order (Company/accounts/warehouse/items/supplier → PO → receipt → invoice → payment as applicable). Use fixed posting/transaction dates, fixed currency and tax settings, explicit naming series, stable external scenario IDs stored in coordinator mapping, and a deterministic seed ledger. Keep events and hidden expected outcomes in the coordinator, not ERPNext.

For repeatable runs, restore a golden site backup (or provision isolated disposable sites from the same image plus deterministic seed manifest). Do not “reset” by deleting submitted ERP transactions or clearing tables: cancellations leave audit/accounting history and DB writes bypass business controllers. Disable or control scheduler/background jobs for benchmark sites so wall-clock notifications/repeats do not produce scenario side effects; advance the business narrative through coordinator events. Verify the post-reset state by comparing expected document counts/IDs, `docstatus`, links, quantities and balances before exposing the run. Database backup/restore and site commands are operational mechanisms; this is an engineering layer still to implement.

## Deployment sizing

Measure the target deployment resources and size the native application stack
before installation. Preserve existing services and configure resource limits
for the chosen environment.

## Authorization and email boundary

Retain existing midPoint as the employee identity/resource authority and Mailu as the single central company mailbox and SMTP/IMAP service. ERPNext users should be provisioned as role-limited application users from those identities (or seeded benchmark identities); ERPNext's User records do not replace employee lifecycle. Keep Frappe's email transport pointed at the existing controlled Mailu/smtp4dev route, with outbound external delivery restricted by current company mail controls. Supplier emails are external business communication and must travel through the simulation's existing mail boundary/coordinator; do not let the test agent send arbitrary real email or allow ERPNext to route around Mailu. Existing employee email/chat, authentication and tenant boundaries remain in midPoint, Rocket.Chat and Mailu.

## Evidence and limits

Primary evidence reviewed: ERPNext/Frappe/Frappe Docker tagged source and license files via the links above; Frappe REST docs; ERPNext Buying, procurement ledger and workflow docs; local Mini hardware and company Mailu/midPoint deployment records in `deploy/single-host/README.md`, `deploy/single-host/company-mail.md`, and `deploy/single-host/midpoint/compose.yml`; local `workspaces/tasks/finance-invoice-matching/{task.md,evaluator.py}` contract. A bounded local clone of ERPNext v15.121.3 was also used to cross-check existing v15 purchase fields; recommended v16 fields were read from the v16.53.0 source tag.

No ERPNext/Frappe services were deployed or benchmarked. Exact memory usage, throughput, Mac Docker architecture/headroom, and whether the proposed draft invoice can be seeded under the final Buying Settings/workflow require runtime validation on the selected pinned images. These are implementation validation items, not evidence that the upstream document/API contract is absent.
