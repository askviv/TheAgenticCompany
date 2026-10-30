# Enterprise simulation requirements traceability

This document maps the original [platform specification](specification.md) and
the simulated-company identity/email brief to the current implementation. It
describes system boundaries and evidence; it does not claim that unrun tasks or
benchmarks have passed. The active design is one shared company and one existing
ERPNext site. A future company with a materially different business persona
requires a separate full deployment; this work does not create another company.

## Requirement-to-implementation map

| Requirement | Current design and implementation | Evidence and limits |
|---|---|---|
| **1. Technology selection** — spec §1 | The Agent Company remains the workplace and employee identity source. ERPNext owns company documents and accounting. The coordinator owns shared company time, event ordering, source reconciliation, company cases and durable evaluations. Mailu is the existing email service; ownCloud and midPoint provide files and identity projections. SimPy schedules deterministic events. Pinned CoffeeBench and E-CommerceBench environments supply company background operations. τ-bench supplies the Retail company-adapted task library and separate native external-provider task domains. | Upstream revisions and selection boundaries are in [business simulator compatibility](business-simulators-compatibility.md), [time/customer compatibility](time-customer-compatibility.md), and [catalogue audit](upstream-catalogue-audit.md). None of the component selections implies that an entire benchmark suite has run. |
| **2. Target architecture** — spec §2 | One persistent company state is shared by concurrent company cases; they can observe the same ERP records and company clock. The coordinator exposes company, background, provider-experiment and evaluation APIs. Background providers have private checkpoints. External-provider cases use the pinned τ worker and a case-bound employee portal/email flow. Runs, evaluations, source events and receipts are durable in the coordinator SQLite stores. | `servers/simulation/{app,engine,store,runtime,company_background,provider_experiments,evaluation_service}.py`; `servers/company/provider_portal.py`. A dashboard Run is not an isolated clone of the ERP site. |
| **3. Truth and identity boundaries** — spec §3 and identity/email brief | ERPNext is authoritative for submitted company orders, inventory, invoices, issues, payment evidence and accounting. Native benchmark state remains private operational/evaluation state. Mailu delivery and authenticated thread evidence prove communications, not financial execution. Company time, source timestamps and real email receipt times are distinct. The existing company persona remains an industrial manufacturer/distributor. Airline, bank and telecom provider runs are explicitly labelled provider-side assistant tasks; they do not relabel the company as an airline, bank or telecom. Viv is the configured financial-operations support actor, not the benchmark assistant being scored. | Shared event/evidence contracts and source-linked mappings are in `servers/simulation/integration_contracts.py`, `feedback_contracts.py`, `company_feedback_runtime.py`, and `company_feedback.py`. Private prompts, model reasoning, native books and credentials stay outside public activity/results. |
| **4A. ERPNext and company finance** — spec §4A | Native Frappe documents and framework defaults are used for source imports and company actions. Source-linked imports preserve original values and employee edits on retry. E-Commerce native monetary units map 1:1 to GBP; marketplace cash is kept in separate simulated bank accounts rather than the company default bank. The approved source ship deadline maps to an ERP dispatch deadline, clearly labelled as a dispatch deadline and not an arrival promise. Drafts and requests do not count as executed financial actions. | `servers/simulation/erpnext.py`; native importer and rollback proofs linked from [verification](verification.md). Unsupported or incomplete mappings remain pending/inconclusive; they do not fabricate money, dates, quantities or success. |
| **4B. Shared clock and events** — spec §4B | The company runs in real time. A shared coordinator clock advances due providers; company cases do not own independent physical clocks. Coffee and E-Commerce provider actions are serialized and persisted with idempotent receipts. E-Commerce's typed global logistics conditions reach Coffee before Coffee advances through the same source window; Coffee's native delivery lag and normal deal/delivery pipeline apply the effect and restore baseline values at expiry. | `servers/simulation/company_background.py`, `background_runtime.py`, `clock.py`, `store.py`; focused coupling, replay and restart tests. No accelerated-time UI is claimed. |
| **4C. CoffeeBench** — spec §4C | The pinned native six-business, 90-day economy runs as a company background provider. The explicitly bound `roaster_A` source transactions can be reconciled as source-linked ERP documents; supported posted ERP evidence and authenticated, exact-thread email can drive corresponding original Coffee actions. Company-mapped Coffee actors are passive with respect to company decisions; they do not decide the company's actions. | CoffeeBench commit `9e2f3950b5812c8753de81ede9c68babc024b5c8`; source notice in `third_party/coffeebench/NOTICE.md`. The original nine TOMLs/21 configured seeds are comparison runs in the source, not 21 separate company assignments or results. |
| **4D. E-CommerceBench** — spec §4D | The pinned native economy is checkpointed and advanced on the shared company clock. Native products, marketplace sales, due/source facts and exact native fulfilment are reconciled through source identities and standard ERPNext records. A submitted, correctly linked Delivery Note can translate to the original native `ship_orders` operation when the source order and quantities reconcile. Marketplace bank facts, refunds, stock/procurement and other lifecycle actions are handled only where native source facts and standard ERP semantics support an exact translation; gaps remain visible/pending. | E-CommerceBench commit `b84678071c424fc94713b0a9e1ae86c519bd74c7`; `third_party/ecommercebench/NOTICE.md`; `servers/simulation/ecommerce_feedback.py`; `deploy/enterprise-simulation/erpnext/admin/ecommerce_finance.py`. No substituted economy or invented ERP fact is used. |
| **4E. τ-bench Retail and external-provider library** — spec §4E | Retail contains 114 company-adapted source tasks and keeps upstream task IDs, policies and evaluators while using company ERP/email evidence. Separately, the provider library exposes 2,432 pinned external-provider tasks: 50 airline, 97 banking and 2,285 telecom. Those cases initialize the actual provider-side task state and score the provider assistant against the native task evaluator; they are not Viv's performance scores and do not make the existing company a bank, airline or telecom. | τ-bench commit `7aad81c37a3a78b4ed3e4866769f851400c64415`; worker source in `servers/simulation/tau2_actor_runtime/`. Retail source gaps remain explicit: eight tasks reference missing source entities. The user explicitly deferred all 200 Intake cases; they are excluded from the runnable library. |
| **5. Combined S01 scenario** — spec §5 | S01 remains a proposed end-to-end company assignment. Its visible employee brief must not reveal hidden timelines or private source facts. Multiple cases may run against shared company records; results must account for shared-record interaction and cannot be treated as independent cloned worlds. | Scenario definitions remain in `servers/simulation/engine.py`. No S01 result or completed end-to-end agent journey is claimed. |
| **6. Employees and counterparties** — spec §6 and identity/email brief | The 17-person roster maps to existing Rocket.Chat and Mailu accounts, Plane identity references, and ownCloud users provisioned through native midPoint projections. Department-specific ownCloud groups/shares enforce employee access. Company Issue notifications go to the responsible employee; if the notification remains unread for five real minutes, a durable handoff sends the employee-authored request to Viv. Viv replies require authenticated SMTP submission identity, the configured Viv address and exact thread. Company requests, native employee notification and provider/Viv correspondence remain separate events. | Identity links and ownCloud policy are documented in [identity/email traceability](identity-email-brief-traceability.md) and native ownCloud verification (private operational report). The five-minute handoff and dedicated authenticated Viv reply relay are implemented but are part of the not-yet-deployed integration described in [verification](verification.md); they have not been exercised with a live Viv interaction. GitLab is excluded. |
| **7. Event/API contracts** — spec §7 | Versioned Pydantic contracts carry stable company/run/generation/source IDs, visibility and timestamps. Mutations use idempotency keys and durable receipts; event and evaluation histories are append-only. Controller/evaluator APIs are separate from agent-facing tools. Email/ERP evidence is authenticated or source-linked before it can trigger native actions. | `servers/simulation/{models,feedback_contracts,integration_contracts,store,evaluation_store}.py`; API and store tests. Failed, incomplete, uncertain and unsupported operations remain distinguishable. |
| **8. Reproducibility and milestones** — spec §§8, 10 | Pinned source revisions, seed, source identity, native checkpoint hashes, company time and safe configuration metadata are retained. Durable evaluation requests reserve before evidence capture and retain native-scale results and private evidence references. Reset is not available for the shared company site; an isolated company reset must not be used for a company-linked case. | Evaluation behavior and API are described in [durable evaluation history](evaluation-history.md). Metadata and deterministic adapter checks do not prove a live task outcome or deterministic model response. |
| **9. Evaluation and attribution** — spec §9 | Results retain each benchmark's native scales and criteria. Public evaluation status is `completed`, `incomplete` or `failed`; a real native zero is a score, while missing evidence is not converted to zero. Company-adapted evaluations preserve the source evaluator where supported and report mapping gaps. External τ scores describe the native provider assistant, not Viv. No single combined percentage is computed across benchmarks. | `servers/simulation/evaluation_service.py`, `evaluation_store.py`, native evaluator adapters and the Results UI. Durable history is implemented; the current integration is deployed; no live task was scored during verification. |
| **10. Engineering principles, deliverables and acceptance** — spec §10 | The integration reuses native ERPNext documents, Mailu, ownCloud and midPoint; preserves the existing company/site; and avoids replacing unrelated workplace data or adding a second secret/mail service. Operator recovery is system-owned; missing implementation is not a new user eligibility gate. | Controlled test and native rollback evidence is summarized in [verification](verification.md). Deployment and scenario acceptance are separate and explicitly reported. |
| **11. Upstream references** — spec §11 | SimPy, CoffeeBench, E-CommerceBench and τ-bench revisions/licenses are pinned and documented with source notices and audit material. | [Compatibility reports](business-simulators-compatibility.md) and `third_party/*/NOTICE.md`. Catalogue size is not a completion metric. |

## Background, company identity and email rules

The existing `The Agent Company` profile and ERP site remain the company identity.
`deploy/single-host/company-identities.json` contains the employee roster, company
background bindings, provider personas, allowed mappings and communication
routing. It is authoritative configuration, not a source of replacement ERP
records. Compatible work is assigned within this company; a future business with
a different persona gets a separate full deployment rather than a second
company silently inserted into this site.

CoffeeBench and E-CommerceBench background providers share the company clock
and contribute source facts through private, replay-safe reconciliation. They
are not manually launched benchmark Runs. Company Activity shows only safe
business events and delivered communication, never private books, prompts,
reasoning or full finance source payloads. A company Run may link to actual
native ERP evidence, but it cannot reset the shared company or claim ownership of
all concurrent changes.

An external-provider case keeps the original provider/task identity and native
task criteria. The portal exposes the configured forms and receives case-bound
customer actions; the handoff email is sent from the configured responsible
company employee. The employee's configured persona and company identity are
not inferred from task content. Only an authenticated Viv mailbox actor on the
exact Message-ID thread can contribute a Viv reply. SMTP authentication proves
the relay identity, not that an ERP action occurred; finance evidence must still
come from native ERPNext events.

ownCloud access is department-based. The current policy assigns employee-owned
documents through the native ownCloud groups and shares; Finance's WebDAV
create/read/update/delete was verified, and Technical access was denied. The
employee-department brief and its folders/assets are documented separately in
[identity/email traceability](identity-email-brief-traceability.md). No employee
files, shares or case attachments are fabricated to make a task pass.

## Catalogue and coverage boundaries

The library counts describe available source definitions, not successful
assignments:

| Library | Source count | Current interpretation |
|---|---:|---|
| τ Retail | 114 | Company-adapted tasks using native company ERP/email evidence; eight have source integrity gaps. |
| τ Airline | 50 | External airline-provider assistant tasks. |
| τ Banking | 97 | External bank-provider assistant tasks. |
| τ Telecom | 2,285 | External telecom-provider assistant tasks; 1,128 financial and 1,157 operational classifications. |
| τ Intake | 200 | Explicitly deferred by the user; excluded from the runnable library. |
| CoffeeBench | 9 TOMLs / 21 configured seeds | Configurations for comparisons within one native 90-day economy, not a task library. |
| E-CommerceBench | One configurable native 365-day economy | A background/source provider, not a discrete task list. |

The 2,432 external τ tasks are 50 + 97 + 2,285. The 10 mock/test τ tasks are
not assignments. Original task IDs and criteria are preserved. Missing source
entities or unsupported state translations remain visible as integrity gaps,
unmapped actions, pending accounting or incomplete evaluations; they are not
filled with guessed records or silently counted as success.

## Results and evidence interpretation

The coordinator reserves durable evaluations before capturing evidence, stores
private snapshots separately from public result metadata, and uses original
native evaluators when supported. Results are not recomputed from current ERP
state after capture. A retry with the same idempotency key returns the original
attempt; a deliberate new evaluation uses a new key and snapshot.

See [verification](verification.md) for current deployment boundaries, test
counts, native proofs and the list of actions that were deliberately not run.
