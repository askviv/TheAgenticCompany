# The Agentic Company dashboard

This deployment supports one company, using the existing company identities and
native ERPNext site. Starting the services does not create or reset an ERP site.
An experiment adds a case and its conversations and events to this company.
Several cases may be active together and observe changes to the same ERP records.
The company also has clock-driven CoffeeBench and E-CommerceBench source providers;
see the [background and finance operator guide](company-background-finance.md)
for their lifecycle, native ERP projections, privacy boundary, and limitations.

## Operator flow

Open `http://192.0.2.10:8110/` for Activity. The company starts automatically
with the services; there are no company-wide start, stop, pause or resume
controls. CoffeeBench and E-CommerceBench each have independent background
[Start/Stop controls](background-controls.md), which preserve their saved state. The company name is read from `company.name` in
`deploy/single-host/company-identities.json`, alongside the existing employee roster.
The company uses real time. Accelerated time is not implemented: external email,
Viv and reminders retain their actual clocks. There is no company reset button.

Metrics above Live activity count tracked emails sent, emails received, chat
messages and open experiments. Counts cover the experiment activity records,
not every message in every mailbox. Stream filters do not change metric totals.
When the company feed cannot be read, affected metrics are unavailable rather
than zero; any available chat activity remains visible.

Open Experiments to select business areas and benchmarks, read a full brief,
and run an assignment. Workplace assignments retain their Rocket.Chat delivery.
Financial operations cases use the coordinator and company email. The combined
S01 case uses supplier, customer and scheduled-event components together.
Original upstream benchmark names describe the source components; they are not
claims that a standalone upstream benchmark suite has been executed.

Activity includes company emails, tracked Rocket.Chat messages and completed
company actions. It shows actual participant messages, not hidden prompts,
private actor facts, model reasoning or future scenario events. Run detail links
retain each assignment's identity and status. Delivery uncertainty is displayed;
viewing or refreshing Activity does not send messages or advance company time.

## Shared state and evaluation

Experiment state retains independent case, conversation, approval and event
correlation. The ERP company and documents are shared. Concurrent cases can
therefore affect each other's observations. These runs are interactive company
experiments; they must not be reported as independently controlled benchmark
scores. The τ retail task library preserves upstream task instructions and
evaluation criteria while using company ERP/email state; its results are not a
claim that the full upstream benchmark ran unchanged. Background providers use
the same company clock and ERP integration. A company-linked case must never
invoke the isolated ERP reset path.

## Deployment and credentials

The coordinator's private `company_binding` configuration refers to the existing
native site, document IDs and credential-file reference. It contains no new
account credentials. The experiment dashboard calls the coordinator server-side
using its dedicated controller-token file, mounted read-only. No coordinator
credential is sent to the browser. Keep the dashboard on the intended trusted
LAN; it retains the existing operator-access model and CSRF protection.

The coordinator and dashboard SQLite migrations are additive. Back up both
SQLite databases and runtime configuration before deployment. Code rollback does
not undo messages already sent or ERP changes; those require operator review.
Stopping the dashboard does not stop the underlying workplace services. Do not
restore an old database over newer company activity as an ordinary rollback.

## Verification boundary

Verify database/API lifecycle, duplicate submissions, shared ERP binding, safe
activity output and UI builds against isolated external adapters. Deployment
checks may read health, catalogue, company status and activity. Service startup
may initialize company metadata and private company-background provider
snapshots. It must not run an experiment, execute a complete upstream suite, or
make an unrequested actor/model call. Do not press Run as part of integration
verification, and do not invoke Viv or start an actual business scenario unless
explicitly requested.

For a fresh coordinator bootstrap, pass `--company-binding /private/company-binding.json`.
The binding JSON is a trusted operator input with this shape (document values
must come from the existing site manifest, never invented fixture replacements):

```json
{
  "site_name": "erp-sim-<existing-site-uuid-without-hyphens>.localhost",
  "credentials_ref": "/private/configured-credential-root/existing-site.json",
  "documents": {
    "company": "existing-company-name",
    "supplier": "existing-supplier-id",
    "item": "existing-item-code",
    "warehouse": "existing-warehouse-id",
    "purchase_order": {"name": "existing-order-id", "item_row": "existing-row-id"},
    "purchase_receipt": "existing-receipt-id",
    "purchase_invoice": "existing-invoice-id",
    "customer": "existing-customer-id",
    "customer_order": "existing-sales-order-id"
  }
}
```

Bootstrap preserves an already configured binding when the option is omitted.
The deployed binding uses the existing site manifest under
`enterprise/erpnext/runtime/`; the public ERP hostname continues to point to
that same site. Missing or invalid native configuration is an operational error,
not a reason to create another company or replace records.

## Prior deployment verification — 30 October 2026

The following records the prior dashboard deployment checkpoint. The shared
background providers, financial source reconciliation, and full τ retail task
catalogue are later integrations; this checkpoint does not establish their
deployed operation or any live background/model/τ execution.

The deployment uses `erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost`, the
same site behind `https://erp.company.example.test`. A read of the existing native
purchase order returned 200 before deployment. Coordinator health returned
`status: ok`; authenticated company status returned `running`, that same site,
and zero company experiments. Unauthenticated company status returned 401.
No company experiment, actor conversation or Viv test was launched.

Focused dashboard/client/company integration checks passed (33 unittest tests, including the direct library-link regression).
Store/API/runtime regression checks passed (41 pytest tests), and bootstrap
checks passed (8 tests). Frontend TypeScript/Vite build and lint completed; lint
retains existing effect-state/component-export warnings.

The broader coordinator/ERP suite returned 99 passed, 2 failed and 3 subtests
passed. Both failures also reproduce from unchanged commit `e30ca60`:

- `test_canonical_supplier_market_event_updates_only_its_private_actor_and_audits_outcome`:
  quantity 11,998 versus expected 9,500.
- `test_e1_requires_grounded_unresolved_serial_finding_before_evaluation_pass`:
  evidence check remains false where the test expects true.

These failures were not suppressed and scoring criteria were not changed. They
limit any claim of verified supplier-market/customer judging behaviour. The
integration deployment is not evidence of a completed business experiment.

Private predeployment backups are under
`/data/theagentcompany/enterprise/backups/shared-company-20261030T005716Z/`.
The prior dashboard image is `tac-experiments:pre-shared-company`. Backups include
source, Compose configuration, runtime JSON and consistent SQLite snapshots.
Code rollback should retain current databases; historical snapshots are for
reviewed disaster recovery and may omit later business activity.

Final dashboard reads returned 200 for `/`, `/experiments`, `/runs`,
`/api/health`, `/api/company`, `/api/activity` and `/api/catalog`. Company status
was running with no error and zero experiments. The dashboard catalogue contained 111 entries,
including A1, B1, C1, D1, E1 and S01. Activity returned four existing tracked
messages with no company error. All three pre-existing dashboard runs remained
present. The served bundle contains Activity, Chatter and Pause company; the tab
title is Activity · The Agentic Company. Direct navigation to the new Experiments
route is covered by a Flask/static-entrypoint regression test.

Visual browser inspection was unavailable because local browser use was disabled
in the Work settings. The checks above establish deployed HTTP responses,
compiled assets and preserved state, not a visual browser interaction or an
executed experiment.

The 111-entry value above is the historical dashboard catalogue at that
checkpoint. The upstream τ retail library is a separate private worker catalogue
of 114 source tasks; it is documented in
[`company-background-finance.md`](company-background-finance.md) and must not be
reported as 114 successful dashboard scenarios or completed evaluations. The
later worker/site-admin integration remains subject to deployment verification.
