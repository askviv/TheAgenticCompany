# Implementation review

The extension was implemented by Luna agents and reviewed by the primary agent. This review covers the new simulation package, pinned ERP deployment, private host runtime, native mail reuse and reference trajectory. It does not claim a measured Viv result.

## Integration and ownership

ERPNext owns purchase orders, goods receipts, stock, invoices and payment records. The coordinator owns physical events and simulated time; supplier/customer actors own their private records and proposals. Actor messages cannot invent ERP transactions. A freight acceptance requires an approved native service purchase order, preserving the component order's original terms. The existing workplace services and benchmark scoring remain separate.

The coordinator uses existing frameworks and native interfaces: FastAPI/Pydantic for contracts and authentication, SQLite transactions/WAL for durable state, SimPy for discrete event ordering, IMAPClient for native mailbox notifications, SignalR Core for smtp4dev notifications, and Frappe document/workflow/backup APIs. Selected E-Commerce Bench source is pinned with its original license and checksum. The isolated Coffee worker uses the upstream CoffeeBench Agent/tool harness, while the isolated customer worker invokes upstream τ-bench `Task`, `UserSimulator`, `UserState`, `ActionEvaluator` and `CommunicateEvaluator` APIs. Both workers use the shared Codex CLI model transport through a custom LiteLLM adapter; the worker runtimes and source pins are documented in the compatibility and requirements reports. One neutral provider transport probe returned a response, but no actor conversation or tested-agent run is claimed.

## Authorization and data scope

Controller, evaluator and agent principals are separate private bearer credentials. The exported agent client contains only its own credential. Agent API access is run-scoped; clock, event injection, reset and evaluation are privileged. The private invoice-assessment helper is not an agent answer oracle. Native ERP writer credentials and Docker access remain on the trusted host, outside the tested agent.

The simulated finance manager evaluates requests against versioned scenario policy and native ERP state. A sender header or “approved” prose never becomes a grant. Grants bind the run, entity, native version and exact terms, and are checked again at the native action boundary. Mailbox ingress accepts correlated Viv requests through the same policy path as API-delivered mail.

Case findings are observations written by the agent. Incorrect or incomplete findings are accepted and judged privately; the case editor does not disclose hidden correctness by rejecting a wrong answer. Native entity ownership and authorization checks remain enforced.

## Lifecycle and operational integrity

Each run owns its ERP site, golden backup, coordinator generation and operation records. Reset uses native Frappe restore, preserves prior audit and shared messages, and never invokes the workplace's global reset. Run locks protect cross-process transitions; SQLite idempotency and native transaction markers reconcile ambiguous results. SMTP crosses a recorded submission boundary before sending, and uncertain delivery is reconciled rather than blindly repeated.

Unexpected errors retain causes and safe operator diagnostics. Provider bodies, credentials and rejected request values are excluded from API errors. Worker failures and uncertain delivery appear in health. Affected operations fail explicitly; findings and unrelated runs remain available.

## Material corrections from review and native checks

- Corrected the official SimPy source/version rather than relying on an obsolete GitHub mirror.
- Aligned the Frappe seed and adapter with the native schema: freight approvals use a run-owned unique Purchase Order marker field, and document submission sends the complete native document required by the installed API.
- Set the Frappe session timezone to UTC and normalize native time values that omit zero-padding, so simulated arrival timestamps round-trip through receipt posting and readback.
- Bounded transient smtp4dev confirmation-read retries inside the notification wait loop. Retries reconcile the already-started Message-ID and never resubmit SMTP; authentication and validation errors still fail immediately.
- Removed the agent-visible invoice assessment oracle.
- Preserved complete canonical event identity and per-run reset/idempotency scope.
- Fixed stale cached outbox snapshots after worker reconciliation.
- Matched native folded Message-ID headers without accepting unrelated candidates.
- Corrected actual SignalR callback arguments and completed-delivery health reporting.
- Corrected the ERP published address and used the host CA trust store for Mailu.
- Tied receipt posting dates to verified simulated arrival, using a historical clock anchor compatible with native Frappe date validation.
- Separated expedited freight from D1 unit-price changes and added a native service-order commitment.
- Judged historical investigation/stock evidence against its observation time rather than the final reconciled balance.
- Required E1 to record unresolved serial evidence and remain in review.
- Kept wrong case findings writable so evaluation can detect errors without coaching the tested agent.
- Corrected reference-run notice matching and immutable clock resumption.

## Evidence and limits

See [verification](verification.md) for actual provider checks and final trajectory status. Five independent acceptance journeys exercise the real engine/store/clock/actors with fake external provider boundaries; they do not establish native provider compatibility alone. Native A1, reset, mail and S01 checks are reported separately.

The host coordinator is privileged because it administers isolated ERP sites through Docker; its host and private files are an operator trust boundary. Start with one assignment on the host and measure before increasing concurrency. Financial tools expose approved goods receipt and freight purchase-order commitments; no payment or refund posting tool is exposed. The reviewed invoice workflow retains a draft invoice for review, and no payment execution is claimed. Environment actors are model-backed through upstream actor frameworks, but inference was not executed; model cost and model/harness performance remain unmeasured. Metrics separate agent model usage from environment-actor usage, record human interventions when provided, and mark unknown cost as unmeasured rather than zero. See [requirements traceability](requirements.md) and [integration verification](verification.md) for scope and limits.
