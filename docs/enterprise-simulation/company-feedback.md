# Company evidence and benchmark feedback

ERPNext and delivered company mail are authoritative business evidence. Native
benchmark databases are private evaluation/operational projections. Viv keeps
using ERPNext and email; no benchmark tool API is exposed to it.

## Shared contracts and persistence

`feedback_contracts.py` defines RecordMapping, CompanyEvidence, TranslatedAction,
TranslationResult and NativeActionReceipt. All adapters share the same exact
field reader, observation ledger, record/thread mapping and action lifecycle.

The existing private coordinator SQLite database stores immutable starting
snapshots, pinned upstream versions/source IDs, ERP document identities, optional
experiment IDs, source/company/real timestamps, email Message-IDs and supporting
links. Reconciliation never restores a source snapshot over employee edits.
Native amendments, cancellations, posted transactions and communications remain
separate facts. A promise, draft, approval or request cannot become execution.

Native Frappe Webhooks capture on_update, on_submit, on_cancel,
on_update_after_submit and on_trash for Purchase/Sales Orders, Purchase Receipts,
Delivery Notes, Purchase/Sales Invoices, Payment Entries and Journal Entries.
They are restricted to the existing `The Agent Company` ERP company and run on
Frappe's after-commit short queue. The receiver verifies the native HMAC over the
exact request bytes. No bearer secret is put in webhook logs. A bounded,
durable cursor through native Webhook Request Logs recovers interrupted delivery.
ERP timestamps and their configured timezone remain distinct from the company
clock; the latter records when evidence was observed, not a fabricated original
simulation timestamp. Previously observed native records supply predecessor
snapshots; missing predecessor evidence remains inconclusive.

Existing mail ingestion supplies gateway-delivery receipts for company-authored
messages. A Coffee message can be linked only when its outbox record carries an
exact mapped ERP document, its recipient is the profile-bound native Coffee
counterparty mailbox, and the sender matches the configured company mail actor.
Replies must retain the same document reference and the exact Message-ID thread.
RFC From is a sender claim, not proof of authenticated company action. Internal
employee-to-Viv handoffs never become supplier messages. Coffee mailbox names
are simulator-owned aliases in the existing Mailu domain; they are not upstream
CoffeeBench-provided email addresses or contacts for real counterparties.
The finance importer links those profile-bound aliases to native Customer or
Supplier records through Frappe Contacts and Dynamic Links. It reuses an exact
email only when that Contact already links to the same party; an address linked
elsewhere fails visibly and existing contacts are not rewritten.

Each action is keyed by SHA256(mapping_id + ':' + evidence_id). Private OS file
locks serialize competing action dispatchers and native Coffee provider ticks.
Native checkpoint and action receipt commit atomically in the existing worker
stores. A restart reconciles that receipt, or retries the identical uncommitted
request. It cannot create another native payment, refund or shipment. An uncertain
transport result remains visible until reconciled. Provider failures are reported
without preventing independent providers' pending actions from being processed.

## Current benchmark coverage

| Provider | Connected native operations | Explicit limits |
|---|---|---|
| CoffeeBench | Exact full posted ERP Payment Entry for a linked unpaid AP invoice calls native pay_invoice. Actual pending offers to the mapped roaster import as source-linked draft Sales Orders with exact item, quantity, unit price and payment terms; submitting the exact mapped order can call native accept_offer. Source-linked, authenticated company emails to explicit Coffee mailbox identities call native send_message and verify the counterparty wake. | Draft offers remain unaccepted until the company submits the mapped order. Mail recipient aliases are simulator-owned, not provided by the upstream benchmark. Header-only inbound messages cannot author native company actions. Other accounting/stock actions remain unmapped. |
| E-CommerceBench | Durable original economy, calendar/daily triggers, source product/marketplace/pending-sale import, and exact submitted Delivery Note fulfilment call native ship_orders. Native cancellation/refund outcomes retain linked source IDs where the native evidence permits it. | Partial fulfilment and semantically different payment/withdrawal or return/warehouse actions remain unmapped. Native cancellations/refunds are reported as ERP reversals not applied; they do not silently cancel orders or post credit notes/payments. Stock-ledger and procurement coverage are incomplete. |
| τ Retail | Run-scoped checkpoints start from actual ERP observations. Supported pending-order cancellation requires posted refund evidence where money was paid. Linked Return/Exchange Issues invoke the original request-registration tools; they do not claim a refund was completed. Frozen ERP observations and the real email transcript go to original task evaluators. | Address observation is not yet company-scoped through Webhooks. Item/payment changes and completed return/exchange settlement translations remain unmapped/inconclusive. Existing ERP state evaluation still reports its original supported criteria independently. |

Mapped Coffee company actors are passive: their model cannot independently make
the same company decisions. External native actors remain benchmark controlled.
Coffee returns its original audit/true_net_income only after its native horizon;
an active horizon reports incomplete without a score. E-Commerce evaluation calls
the original get_analysis_report, rather than inventing scoring rules. τ uses the
pinned original task/evaluator criteria; criteria with ambiguous shared records
return inconclusive with no reward.

The company JSON explicitly records the two approved E-Commerce adaptations:

- One native monetary unit maps to GBP 1 (`money_unit`).
- The exact source ship deadline becomes the ERP fulfilment delivery_date
  (`sales_order_date_semantics=source_dispatch_deadline`). Header/item dates,
  original ship-by field, visible date-meaning field and terms all retain
  **dispatch deadline, not arrival promise**. The adapter invents no date.

The native source exposes dates, not transaction instants. Source sale_date is
preserved exactly; occurred_at is explicitly its first company observation.
Coffee and E-Commerce native clocks have minute precision; receipts retain the
original company timestamp and identify native minute conversion. Stale evidence
is reported inconclusive, never replayed by rewinding a benchmark.

## Results and attribution

Private `GET /company/feedback?experiment_id=...` requires controller/evaluator
authentication. It separately reports integration receipts/failures, actual
business evidence, native results, coverage and shared-company interactions.
It contains financial evidence and must not be exposed as an agent endpoint.
Shared records are never reset for an experiment. Overlapping record changes
whose ownership is ambiguous suppress attributable grading. Sharing a record
without changes does not itself count as failure. Existing experiment evaluation
includes the ledger summary without running another native/model evaluation;
ambiguous results are inconclusive, partial native criteria unevaluated.

Results are labelled **company-adapted evaluation**. The adapters do not claim
that all original benchmark actions, worlds, scoring assumptions or financial
books have been preserved. Activity outside a mapping does not earn native credit.

## Operation and integration evidence

The existing tac-company-issues timer runs recovery/reconciliation. Stop it and
outstanding handoff timers with the coordinator before restoring Kubernetes;
see the coordinator deployment guide. Preserve both the coordinator and actor
SQLite stores plus their private native checkpoints when backing up/restoring.
No Kubernetes workload, PV or database is replaced by this feature.

Verification uses synthetic records, native components and captured transport
boundaries only. Native Frappe webhook rendering/signing was rolled back with
zero residual Webhook changes. The native E-Commerce GBP/dispatch-date import
replayed idempotently, remained draft, created no GL entries, and rolled back all
fixture source rows. Native Coffee action concurrency/restart, E-Commerce shipping
and τ Retail checkpoint/refund/cancellation tests use original upstream code.
No live experiment, Viv task or model performance run is part of verification.

Native Issue notifications join the financial document observers (42 owned Webhooks in total). The signed receiver durably queues evidence before returning; translation and native action reconciliation run in the existing maintenance service. Missing posted financial proof remains inconclusive and can be reconsidered when supporting evidence arrives; accepted native actions are never repeated.
