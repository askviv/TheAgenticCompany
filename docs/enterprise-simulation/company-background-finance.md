# Shared company background and finance operations

**Status:** deployed shared-company integration; see [verification evidence](background-retail-verification.md). No live background model turn or completed τ task is claimed.

The company owns one real-time clock, one ERPNext site, its existing employee identities, and the existing Mailu installation. CoffeeBench and E-CommerceBench run as company-scoped source providers. They are not separate benchmark companies and their private books never replace ERPNext as the company's financial record.

## Operator lifecycle

The service manager starts `CompanyBackgroundService` with the coordinator. On startup it initializes durable provider snapshots from the company time and configured company bindings. The Coffee provider starts at the UTC midnight anchor for the current company day; its first native actor wake is scheduled by the provider. E-Commerce is advanced at its next calendar boundary. There is no manual benchmark Run action for either background.

The company clock wakes the background service when its next due time arrives. Provider calls run on the background service thread, independently of an experiment's clock thread. A provider error is visible in company status and retried; a failure in one provider does not make its result look successful. Coffee's native horizon is 90 days. At the horizon the provider settles its native terminal deliveries, reports `finished`, and stops scheduling further Coffee work. It does not silently reset or start another world.

Coffee initialization requires the explicitly configured background model ID to
match the supplier worker's model binding. A missing or mismatched ID is a
configuration error, not an implicit fallback. Model turns may occur when native
Coffee actor work becomes due; health/catalogue checks are not evidence that
those turns ran or that the 90-day world completed.

Company background status and due-obligation observations contain operational summaries. Private actor prompts, model reasoning, source-book snapshots, and full finance source facts are not Activity entries. Public company source events carry only the allow-listed market date/event metadata and provider provenance. Receipt document names and monetary facts belong to the trusted finance path.

## Provider responsibilities and limits

| Provider | What the company integration runs | What it does not create |
| --- | --- | --- |
| CoffeeBench, pinned at `9e2f3950b5812c8753de81ede9c68babc024b5c8` | The actual upstream six-business economy, event dispatch, actor turns, deal and invoice lifecycle, and source facts where a transaction involves the explicitly bound company business (`roaster_A`). A private native checkpoint and turn-response write-ahead store preserve progress across worker restart. | A second company ledger, guessed counterparties/amounts, benchmark score, or automatic ERP payment. The original nine TOML experiment configurations (21 configured seed runs) are not separately executed as nine company tasks. |
| E-CommerceBench, pinned at `b84678071c424fc94713b0a9e1ae86c519bd74c7` | Native calendar-driven supplier demand and dated supply-disruption signals, applied to the supplier's private external market. | A second company cash, stock, purchase-order, delivery, or accounts-payable ledger. Its source contains no native AP schedule; the provider emits no fake invoice or payable. |
| τ-bench Retail, pinned at `7aad81c37a3a78b4ed3e4866769f851400c64415` | The full 114-task retail catalogue, original task IDs and criteria, upstream τ task loader and retail evaluator, and task-linked source database projection into the company's native ERP/email task flow. | A claim that 114 live tasks were completed or scored. The source data has eight incomplete task rows; missing source entities are surfaced as integrity/evaluation failures, never synthesized. |

Coffee source invoices and orders are reconciled as native draft Purchase/Sales Orders and Purchase/Sales Invoices. Their item quantities and source prices/amounts come from the pinned native source facts. Counterparty and item records are linked to source identity; native ERPNext defaults and dated USD exchange rates are used. A missing required native framework default or exchange rate fails reconciliation visibly. Existing documents are reused by company-scoped source identity and are never overwritten on retry, so subsequent agent edits remain intact.

A Coffee payment appears as native payment evidence (an ERPNext Issue with the immutable source snapshot) because the upstream actor ledger does not identify a company bank account or authorize an ERP posting. It does not become a Payment Entry or a posted bank transaction. Draft orders/invoices remain subject to the ordinary ERPNext review, approval, and submission flow. Due/overdue obligations are read from submitted native invoices; E-Commerce does not manufacture them.

## Private contracts and retry behavior

The worker RPC contract is in [`integration_contracts.py`](../../servers/simulation/integration_contracts.py). It uses versioned initialize/advance requests and responses with provider ID, pinned upstream commit, cursor, status, next due time, events, outcomes, and finance source intents. The provider identity values are `sakanaai-coffeebench-native-economy` and `qwenlm-ecommercebench-calendar`.

The worker records an operation claim before advancing native state or making an actor turn. A repeated operation ID with the same request returns its durable result; a conflicting request fails. An operation with a claim but no confirmed commit is reported as uncertain and is not repeated as new model work. Coffee snapshots are private worker-owned pickle files; RPC callers cannot choose checkpoint paths or receive raw checkpoint bytes. Treat checkpoints as trusted, private state and never load externally supplied pickle data.

Company finance source intents are persisted before calling the private ERP site-admin operation. Each record keeps its upstream source ID, provider commit, source kind, party, currency, amount, occurrence date, and full native record. ERP receipts are recorded only when source IDs match the pending intent set. If ERP reconciliation fails after document creation, retry uses the shared [`source_records.py`](../../deploy/enterprise-simulation/erpnext/admin/source_records.py) identity fields to reuse the existing document without restoring its original values. Unknown provider provenance fails visibly; the known E-Commerce partition is intentionally not sent to Coffee finance import.

These records and source snapshots are private company state. Do not put them in agent tools, public Activity, browser responses, support logs, or analytics exports. Agent-visible email and document observations use existing authorization, document permissions, and Mailu routing. A statement in email is not proof that a payment or approval occurred.

## τ retail and email/ERP compatibility

The τ worker uses the upstream `Task` and `UserSimulator`, original English retail `tasks.json`, `db.json`, `policy.md`, and `split_tasks.json`, all hash-checked from the pinned commit. It exposes the complete task list and task-linked manifests privately. The seed-email endpoint runs the original scenario through the same τ user simulation and configured Codex transport as the normal actor turn path. The native retail evaluator receives a projection from ERP-observed task records and the τ message trajectory; original action, communication, and applicable natural-language evaluation criteria remain in the upstream task/evaluator path.

The source catalogue includes 114 tasks, 50 products, 500 users, and 1,000 orders. Eight tasks cannot be fully grounded in the pinned database: tasks 2–4 reference missing product `6086499569`; task 39 and tasks 67–68 have no matching customer for their lookup; tasks 46–47 reference absent orders `#9502126` and `#9502127`. The runtime reports those source integrity problems rather than filling in records. No full τ run, email conversation, or Viv task is part of this documentation or its validation.

This integration is not a reproduction of all upstream suites. The Coffee company provider uses CoffeeBench's full economy and actor runtime but does not claim the original experiment runner's comparisons or metrics. E-Commerce contributes actual calendar market effects, not its full 365-day retailer/order/fraud/settlement benchmark. τ Retail preserves the task/evaluator source while projecting observations into the existing company ERP and email boundary; it is not an untouched τ reference database execution. The full catalogue must not be represented as 114 already-passed tasks.

## Rollback and recovery

Code rollback does not reverse source reconciliation. Keep current coordinator SQLite, worker SQLite/checkpoints, company ERP documents, and Mailu state when rolling back application code. Do not restore an older SQLite snapshot or golden ERP database over newer company work as routine rollback. Source-linked documents are ordinary ERPNext records and may have been edited or acted on; have an operator review them using their hidden source identity before any manual correction. Never delete or cancel source documents automatically during code rollback.

The worker and site-admin operation failures retain safe exception types/cause chains in operator logs while omitting private source payloads. Recovery is an operator/system action; it is not a new user readiness gate. No deployment, live model turn, full company-world episode, financial posting, or Viv execution should be inferred from component tests.

## Shared logistics conditions

The company JSON explicitly enables `background.coupling.ecommerce_to_coffee`. The reusable `ExternalSupplyCondition` contract carries the original E-Commerce event ID, pinned source revision, global category scope, multiplier, start and expiry. E-Commerce confirms its source window before Coffee advances. Durable pending requests retain the exact condition batch across failures and restarts; a failed source leaves Coffee visibly waiting while unrelated company services continue.

The coupling applies upstream global logistics events (`affected_categories: all`) to the native Coffee market's Item delivery lags, using the upstream worst-active-multiplier rule. Native Marketplace deal creation uses those lags; normal deliveries then affect inventory availability and financial activity. Company-mapped actors receive the source condition at their next native morning wake and make their own native pricing and procurement decisions. Expiry restores original native lags. Existing accepted deal commitments remain intact.

Hardware-specific cost shocks do not describe coffee production costs and have no invented commodity conversion. The configured coupling therefore does not force prices, rewrite existing stock or post automatic payments. These are different native product economies linked by applicable common conditions, rather than identical databases. Snapshot schema v2 migrates the existing unconditioned Coffee checkpoint from its actual native Item fields, preserving its books and cursor.
