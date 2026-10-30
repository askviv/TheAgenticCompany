# Enterprise simulation service

This package extends the existing workplace with ERPNext sites, SimPy events, private counterparties, and objective evaluation. Company communication uses the existing Mailu and Viv smtp4dev route. The original isolated benchmark start, reset, and evaluation criteria remain available and unchanged.

## Persistent shared company

When `company_binding` is present in the private runtime configuration, service startup records the shared-company runtime against that preconfigured native ERP binding. This initializes coordinator metadata only: it does not create, reset, or reseed an ERP site, and it does not create an experiment or send an assignment. An existing paused company stays paused across service restarts. A missing binding is reported as a degraded `/health` component; isolated benchmark routes remain available.

`GET /company` reports company state, experiments, and completed public activity. `POST /company/experiments` creates a scenario experiment in the persistent company. Experiment cases, evaluation evidence, and message correlation remain scenario scoped, while all experiments use the same ERPNext site, native stock and orders, configured people identities, and wall-time company clock. `POST /company/pause` stops company clock/event and autonomous mail processing; `POST /company/start` resumes it. Company experiments cannot be reset individually because their ERP state is shared.

The evaluator keeps each scenario's original checks. In shared-company mode those checks read current native ERP state, which may include another experiment's ERP actions; evaluation responses identify this context and list peer experiments. Shared-company results therefore do not have the independent-state interpretation of isolated benchmark results.

See the [architecture and compatibility audits](../../docs/enterprise-simulation/README.md), [native verification report](../../docs/enterprise-simulation/verification.md), and [host deployment instructions](../../deploy/enterprise-simulation/coordinator/README.md).

## Agent and custom harness boundary

Give the tested harness only its agent client configuration and the existing workplace credentials it is permitted to use. The coordinator's generated `agent-client.json` contains the agent API URL and its bearer credential; keep the credential in a private secret mount and send it as an Authorization header. The full principals file, evaluator state, actor records, native ERP writer tokens and host Docker access belong to the trusted coordinator.

The initial task arrives as a normal company email. It contains business case and native document references. `X-TAC-Run-ID` and `X-TAC-Generation` correlate the experiment; preserve the normal `Message-ID`, `In-Reply-To` and `References` thread headers when replying by email. The agent does not receive the hidden timeline or expected answer. Company employees, suppliers, customers and the finance manager respond using the same company mail infrastructure.

A custom harness can use these authenticated operations. The running API's `/docs` describes the exact Pydantic request schemas.

| Operation | Endpoint |
| --- | --- |
| Read the agent's native inbox | `GET /agents/{actor_id}/inbox?run_id={uuid}` |
| Inspect a visible case | `GET /cases/{case_id}?run_id={uuid}` |
| Record findings and case updates | `PATCH /cases/{case_id}?run_id={uuid}` |
| Send a native company email | `POST /messages` |
| Read permitted ERP records | `GET /runs/{run_id}/erp/{operation}/{entity_id}` |
| Request native invoice review | `POST /runs/{run_id}/invoice-review/{invoice_id}` |
| Execute a supported approved ERP action | `POST /runs/{run_id}/erp/actions/{operation}` |

Permitted ERP reads cover purchase orders, receipts, invoices, suppliers, customer sales orders, customers, stock and payment status. Stock reads use the run's warehouse and take the item identifier as `entity_id`. The private invoice-assessment helper is not an agent tool: the agent must inspect the underlying business documents and record its finding.

Mutations require `Idempotency-Key`. Keep the key stable when retrying the same request; a changed request needs a new key. An uncertain SMTP result means delivery may have occurred. Reconcile the saved operation rather than sending another email with a new identifier. Provider failures return a safe failure and correlation ID; they are not successful agent actions.

Consequential ERP actions consume an exact finance-manager grant tied to the run, business entity, native version and approved terms. Sending an email saying “approved” does not create a grant. The simulated manager applies the scenario's versioned policy to native ERP records, recorded physical events and the actual request. The trusted coordinator validates that decision again at the action boundary.

## Upstream actor services

Customer turns use the pinned τ-bench `UserSimulator`, Task/UserState and native evaluators. Supplier turns use CoffeeBench's actual Agent/tool loop with selected E-Commerce Bench negotiation, demand and disruption code. Separate locked worker environments connect through authenticated loopback RPC; the coordinator persists private request snapshots and reconciles the same turn ID after uncertainty. There is no template fallback. The model transport is the user's Codex CLI login, configured as `gpt-6-luna`; actor conversations and Viv performance were not run for this handover.

See [requirements and configuration](../../docs/enterprise-simulation/requirements.md) and [actor deployment](../../deploy/enterprise-simulation/actors/README.md).

## Controller and evaluator

The controller starts/reset scenarios, injects canonical events, advances/pauses simulated time and reconciles mail. The evaluator inspects private audit and native business state. Neither role is included in the agent client configuration. Controller-only telemetry endpoints record model usage and human intervention; evaluator-only trajectory export includes current and retained prior generations. Costs for the tested agent and environment actors are kept separate, with unmeasured values explicitly null. Only the controller advances time; waiting for real email delivery does not advance a simulated business day.

The reference runner exercises production engine and provider paths with a scripted actor. Its report is infrastructure evidence, not a Viv/model performance result. A/B/C harness comparisons should record the same model identifier, prompts, tools and seeds while changing only the intended harness capabilities. Report unmeasured costs as unmeasured rather than zero.

## Reset and concurrency

Each run owns an ERP site and coordinator generation. Reset uses that site's native golden backup, preserves prior audit history, changes message generation and leaves other runs intact. Shared workplace resets and mailbox deletion are not part of reset. Old native messages remain in the shared inbox but cannot be consumed as the new generation's evidence.

Distinct runs can progress independently. Run-scoped process/thread locks and durable idempotency protect state transitions. the host's limited CPU makes native provisioning and restoration slow; measure resource use and begin with one assignment at a time.
