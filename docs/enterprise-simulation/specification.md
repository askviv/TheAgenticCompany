# Design specification: Enterprise Agent Simulation Platform

Technical implementation brief · Version 1.0 · October 2026

Objective: Extend The Agent Company into a realistic enterprise simulation where AI agents perform long-running business work involving employees, customers, suppliers, ERP systems, changing events, financial consequences and human approval requirements.

The environment must allow you to evaluate an agent's ability to complete real business processes, not merely execute isolated tool calls.

## 1. Final technology selection

I would include all six components, but give each one a clearly defined responsibility.

| Component                                                                                      | Role                                                                                   | Priority |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | -------- |
| [The Agent Company](https://github.com/TheAgentCompany/TheAgentCompany?utm_source=chatgpt.com) | Primary workplace: employees, chat, files, projects and tasks                          | Core     |
| [ERPNext](https://github.com/frappe/erpnext?utm_source=chatgpt.com)                            | ERP system: purchasing, inventory, invoices, accounting and customer records           | Core     |
| [SimPy](https://simpy.readthedocs.io/?utm_source=chatgpt.com)                                  | Simulated time, deliveries, delays and asynchronous business events                    | Core     |
| [CoffeeBench](https://github.com/SakanaAI/CoffeeBench?utm_source=chatgpt.com)                  | Independent businesses that negotiate and interact over time                           | Phase 2  |
| [E-Commerce Bench](https://github.com/QwenLM/E-CommerceBench?utm_source=chatgpt.com)           | Supplier behavior, demand, inventory economics, disruptions and longer-term operations | Phase 2  |
| [τ-bench](https://github.com/sierra-research/tau2-bench?utm_source=chatgpt.com)                | Simulated customers, customer service conversations, policies and tool-use evaluation  | Phase 3  |

Important: These are independently developed research projects, not compatible plug-ins. The specification calls for adapters and selective reuse of their components. It does not assume that putting all repositories into one Docker environment will make them work together.

## 2. Target architecture

Agent under test

Your model and harness · Own memory, planning, tools and orchestration

Controlled tool APIs and event gateway

The Agent Company — workplace

Chat

Files

Projects

Coworkers

ERPNext

Orders, stock, invoices, finance

SimPy

Events, deliveries, delays, time

CoffeeBench + E-Commerce

External businesses, negotiations, markets

τ-bench

Customers, conversations, policies

All changes recorded

Shared simulation coordinator and evaluator

Canonical IDs · Event log · Scenario truth · Resets · Objective scoring

This architecture requires a new integration service. I would call it `simulation-coordinator`.

It manages the relationships between all the environments while keeping the agent under test separate from the simulation's hidden facts.

## 3. Key design rule: One system owns each type of truth

This is probably the most important technical decision.

| Information                                                | Authoritative system           |
| ---------------------------------------------------------- | ------------------------------ |
| Purchase orders, invoices, payments                        | ERPNext                        |
| Inventory balances and goods receipts                      | ERPNext                        |
| Real location and movement of simulated shipments          | Simulation coordinator + SimPy |
| Internal messages, files and project tasks                 | The Agent Company              |
| Supplier preferences, finances and negotiation constraints | External-business simulator    |
| Customer goals and private information                     | Customer simulator             |
| Business policies and approval limits                      | Policy service                 |
| Correct expected outcomes                                  | Private evaluator              |

For example, SimPy might know that a shipment of 2,000 components is currently delayed at a distribution center.

The tested agent must not be given that fact directly.

It should have to contact logistics, ask a warehouse employee or inspect a delivery record before discovering it.

Otherwise you're testing whether the agent can read a database, not whether it can investigate a business problem.

Similarly, the supplier simulator must not be able to invent a completed ERPNext payment. Its claims must be reconciled against the authoritative financial records.

## 4. Integration specifications and acceptance tests

### Integration A — ERPNext

Purpose: Add real ERP functionality to The Agent Company.

ERPNext provides purchasing, receiving, stock, accounting and invoicing records, accessed through Frappe's documented REST and RPC APIs.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://docs.frappe.io\&sz=32)

docs.frappe.io



Implementation

Create an `erpnext-adapter` service with permitted operations such as:

- `get_purchase_order`
- `get_purchase_receipt`
- `get_purchase_invoice`
- `get_supplier`
- `get_stock_balance`
- `create_goods_receipt`
- `request_invoice_review`
- `read_payment_status`

Initially, make financial posting actions approval-gated and use supported ERPNext document workflows rather than writing directly to database tables.

Seed a fictional manufacturer with suppliers, products, warehouses, purchase orders and invoices.

Test A1 — Invoice mismatch

Integration test

Initial data: 10,000 components ordered; 8,000 received; invoice claims 10,000.

Agent task: Retrieve the documents, identify the discrepancy and open a review request.

Pass conditions: Correct 2,000-unit mismatch identified; correct purchase order and invoice linked; no unauthorized payment or goods receipt created.

### Integration B — SimPy

Purpose: Introduce time and changing real-world events.

SimPy is a discrete-event simulation framework. We'll use it to schedule outcomes such as shipments moving, employees becoming available, supplier responses arriving and transport disruptions.

Implementation

Create a `simulation-clock` service exposing operations including `schedule_event`, `advance_time`, `get_pending_events` for privileged simulation administration, and `pause_simulation`.

Only the simulation controller can advance time. The tested agent interacts through normal business channels.

Use explicit event types such as `SHIPMENT_DELAYED`, `DELIVERY_ARRIVED`, `EMPLOYEE_RESPONSE_READY` and `PAYMENT_POSTED`.

Test B1 — Delayed shipment

Integration test

Initial data: An expected delivery is due on simulated Day 2.

Injected event: Transport delays the delivery until Day 5.

Agent task: Notice the delay through an allowed communication channel, follow up, and update the relevant stakeholders.

Pass conditions: Correct event processed once; delivery is not prematurely recorded; the agent tracks the changed ETA.

### Integration C — CoffeeBench

Purpose: Add independent businesses that have their own goals and information.

CoffeeBench models a 90-day economy involving coffee growers, roasters and retailers, with separate AI agents operating the businesses.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub



Implementation

Do not assume its coffee businesses can automatically become automotive suppliers.

Instead, inspect and adapt its actor scheduling, business-to-business messaging, negotiation and economic-accounting patterns.

Implement an `external-business-service` that supports supplier identities, private stock availability, quotations, disputed deliveries and negotiated commitments.

Give each business its own policy, model configuration and restricted access to information.

Test C1 — Supplier disagreement

Integration test

Initial data: Company claims 2,000 units are missing. Supplier believes it dispatched the full quantity.

Agent task: Contact the supplier and request supporting documentation.

Pass conditions: Supplier replies based on its own records; agent captures the evidence; conflicting claims are not silently reconciled or overwritten.

### Integration D — E-Commerce Bench

Purpose: Add sustained market activity, supplier economics, fraudulent or unreliable suppliers and changing inventory conditions.

E-Commerce Bench models 365 days of store operations using inventory, negotiation, returns, cash flow and market events. Its supplier decisions are driven by a deterministic negotiation kernel, even though dialogue can be rendered by an LLM.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub



Implementation

Reuse suitable demand, negotiation and disruption logic through adapters. Do not run its entire store economy as a second authoritative ERP beside ERPNext.

Translate its economic events into canonical events accepted by the simulation coordinator. ERPNext remains authoritative for company purchases, inventory and invoices.

Test D1 — Supplier price change

Integration test

Initial data: A supplier quotes £50 per unit.

Injected event: An upstream supply disruption changes its future acceptable pricing.

Agent task: Request a revised quotation, compare it with the agreed purchase order, and route any contract change for authorization.

Pass conditions: The revised offer is correctly recorded; the original contract is preserved; no unauthorized price change occurs.

### Integration E — τ-bench

Purpose: Add realistic customer conversations, partial information, policy constraints and customer-service scenarios.

τ-bench provides simulated users, domain-specific tools, policies, scenario definitions and grading. It supports custom environments and agents through its orchestration interfaces.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

GitHub+1



Implementation

Create a custom domain connected to your company's customers and ERP records.

Reuse its user-simulation and policy-evaluation approach. The conversational user should have private scenario details and should reveal information in response to appropriate questions.

For the integration test, a customer disputes a shipment and requests a refund.

Test E1 — Customer complaint

Integration test

Initial data: Customer reports damaged components but hasn't provided serial numbers.

Agent task: Ask for missing information, inspect the relevant order and determine whether the case is ready for review.

Pass conditions: Required serial numbers requested; case correctly linked to the order; no unauthorized refund; unresolved evidence clearly identified.

## 5. The first complete end-to-end experiment

After the five individual integrations pass, run one combined scenario that uses every component.

## Scenario S01: The missing shipment and disputed invoice

Illustrative duration: 14 simulated days · Manufacturing and procurement

The simulation timeline

1. Day 1

   The problem appears

   ERPNext contains a blocked £500,000 invoice for 10,000 components when only 8,000 are recorded as received.
2. Day 2

   The supplier disputes the shortage

   The external supplier agent receives a message and claims that all 10,000 were dispatched.
3. Day 3

   The warehouse investigates

   A simulated employee accesses warehouse records and discovers an incomplete receiving record. They may require a follow-up request.
4. Day 4

   New evidence emerges

   A logistics actor locates 2,000 components at another distribution center. The information is available only through appropriate workplace interactions.
5. Day 5

   Production becomes affected

   SimPy triggers a production deadline. The agent must identify whether the missing components create a manufacturing shortage.
6. Day 7

   The recovery plan fails

   A new transport delay invalidates the expected delivery date. The agent must revisit the plan and notify affected parties.
7. Day 9

   The customer changes requirements

   A τ-bench-style simulated customer requests an earlier delivery and asks for clarification about the revised schedule.
8. Day 11

   Commercial terms change

   The supplier proposes expedited shipping at additional cost. The agent must obtain the appropriate authorization.
9. Day 14

   Final resolution

   The missing goods arrive. The agent must verify the final receipts, ensure payment approval conditions are met, close the dispute and inform stakeholders.

The days and events above are a new test scenario you would implement, not an existing scenario shipped with any one benchmark.

### What should the agent actually receive?

Only a normal employee-style instruction:

> "Please investigate the blocked supplier invoice, resolve the delivery discrepancy, coordinate with the relevant teams and make sure the order can be closed correctly. Escalate decisions requiring approval."

It should not be given the scenario timeline, hidden supplier constraints or the final answer.

The agent has to discover the problem, plan its work, take appropriate actions, wait for replies, respond to new events and eventually close the case.

## 6. How to simulate actual employees

I would create four types of simulated actors.

| Actor               | Example              | Behavior                                                       |
| ------------------- | -------------------- | -------------------------------------------------------------- |
| Internal employee   | Warehouse supervisor | Has access to local warehouse records and responds to requests |
| Manager or approver | Finance manager      | Assesses requests against a defined approval policy            |
| External business   | Component supplier   | Has independent inventory, constraints and commercial goals    |
| Customer            | Vehicle manufacturer | Makes requests, provides evidence and may change requirements  |

Each actor needs:

- A distinct identity, permissions and private information.
- An inbox and mechanism for receiving messages.
- A response policy defining what it can disclose or approve.
- A response delay, availability calendar and possible escalation paths.
- Access only to the documents and systems its role permits.

Separate facts from generated conversation. If the warehouse employee says a shipment was received, that statement must be based on a simulated goods-receipt event, not on a language model improvising.

The language model may decide how the employee communicates or which follow-up questions it asks. The simulator must control whether the underlying facts are true.

You should also distinguish between deterministic employee behavior and LLM-driven behavior in your test reports, because the latter introduces variability.

## 7. Integration contracts your engineering agents should implement

Use a shared event schema and identifier system so the separate simulations can coordinate reliably.

```

{
  "event_id": "evt-000123",
  "scenario_id": "S01",
  "simulated_time": "2026-11-03T09:00:00Z",
  "event_type": "SHIPMENT_DELAYED",
  "source": "logistics_simulator",
  "entity_type": "shipment",
  "entity_id": "SHIP-001",
  "related_entities": [
    "PO-001",
    "SUPPLIER-001",
    "CASE-001"
  ],
  "payload": {
    "quantity": 2000,
    "previous_eta": "2026-11-04",
    "revised_eta": "2026-11-07"
  },
  "visibility": [
    "logistics",
    "warehouse"
  ]
}

```

This is a proposed integration contract, not a schema provided by the existing projects.

The integration layer should support these core operations:

| Interface                       | Purpose                                                 |
| ------------------------------- | ------------------------------------------------------- |
| `POST /scenarios/start`         | Restore a clean scenario and initialize actors          |
| `POST /events`                  | Accept a validated simulated event                      |
| `POST /clock/advance`           | Advance simulated time, restricted to the controller    |
| `GET /agents/{id}/inbox`        | Deliver messages visible to that agent                  |
| `POST /messages`                | Send messages through authorized communication channels |
| `GET /cases/{id}`               | Retrieve the visible state of a business case           |
| `POST /scenarios/{id}/evaluate` | Run the private evaluator                               |
| `POST /scenarios/{id}/reset`    | Restore initial state for repeatable experiments        |

Implementation controls:

- All mutating calls need idempotency keys to prevent duplicate invoices, payments and deliveries.
- Messages and events must be recorded in an append-only audit log.
- All system identities and permissions should be explicit.
- The agent must never receive the private evaluator's state.
- Simulation seeds, model versions, prompts and tool configurations must be recorded.
- The simulation needs deterministic reset and replay where possible.

## 8. Implementation sequence

Suggested engineering milestones, not guaranteed completion dates

1Milestone 1Workplace and ERP

Deploy The Agent Company and ERPNext, configure shared identities, and implement purchase-order and invoice tools.

Acceptance criteria

Agent can authenticate to permitted workplace tools

ERPNext mismatch test A1 passes

Scenario reset produces identical ERP state

2Milestone 2Simulated time and events

3Milestone 3Independent businesses

4Milestone 4Long-horizon economics

5Milestone 5Customer and policy simulation

6Milestone 6Full evaluation

## 9. How you prove that the agent can do the work

Do not measure success solely by whether the final invoice was paid or the goods arrived.

The shipment could arrive successfully because of the simulator's events even if the agent did almost nothing.

You need to determine whether the agent actually performed the necessary work correctly.

| Metric                     | What to measure                                                    |
| -------------------------- | ------------------------------------------------------------------ |
| End-to-end completion      | Did the complete business process reach its expected final state?  |
| Investigation completeness | Did the agent discover and evaluate the critical evidence?         |
| Coordination success       | Did it contact the appropriate people and handle their responses?  |
| Recovery capability        | Did it detect when an earlier plan became invalid and recover?     |
| Policy compliance          | Were actions and approvals within the agent's permitted authority? |
| Human intervention         | How often did someone have to rescue or correct the agent?         |
| Cost and efficiency        | Model cost, tool calls, elapsed simulated time and human effort    |
| State consistency          | Did ERP, workplace and simulator records remain consistent?        |

### The actual experiment: model versus harness

Use the same model, same scenario seeds, same initial records and same simulated counterparties in three setups.

Controlled experiment configurations

A

Basic agent

Model, prompt, tools and conversation history.

B

Agent with persistent memory

Same model plus structured case tracking, persistent commitments and retrieval of past events.

C

Agent with a durable orchestration harness

Same model plus persistent memory, event-triggered resumption, follow-up scheduling, retry handling, approvals and workflow recovery.

Run each configuration multiple times, including different scenario seeds and several variations of unexpected events.

Report completion rates and failure modes, not just a single successful run. Keep the evaluation environment's LLMs and decision rules fixed across comparisons where possible.

This would give you evidence about the effect of the harness rather than conflating improvements in orchestration with improvements in the underlying model.

## 10. Instructions to hand to your implementation agents

# Engineering brief: Extend The Agent Company into an enterprise workflow simulator

## Objective

Build a modular, reproducible enterprise simulation environment using The Agent Company as the primary workplace, integrating ERPNext, SimPy, CoffeeBench-inspired independent business agents, selected E-Commerce Bench economic simulation components, and τ-bench customer simulation capabilities.

The resulting environment must support long-horizon business processes with multiple employees, external parties, changing circumstances, approvals and measurable outcomes.

## Engineering principles

1. Preserve The Agent Company's existing benchmark functionality.
2. Keep integrations modular and avoid unnecessary modifications to upstream projects.
3. Treat ERPNext as the system of record for transactional business data.
4. Treat the simulation engine as the authority for physical events and simulated time.
5. Allow each simulated actor access only to its own information.
6. Separate hidden simulation truth from information available to the tested agent.
7. Require explicit permissions and approval checks before consequential actions.
8. Support scenario resets, deterministic seeds, event replay and complete audit logs.
9. Implement objective evaluations that inspect business state and required actions.
10. Record and test upstream repository versions. Do not assume existing APIs or components can be directly combined.

## Required services

- Workplace service: The Agent Company
- ERP service: ERPNext
- Simulation clock: SimPy
- Integration and state-coordination service: custom implementation
- External-business service: CoffeeBench-inspired actor simulation
- Market and supplier behavior service: selected E-Commerce Bench components
- Customer and policy simulator: τ-bench custom domain
- Scenario runner and independent evaluator

## Required implementation milestones

**M1 — ERPNext:** Deploy and connect ERPNext. Implement procurement tools. Pass invoice-mismatch test A1.

**M2 — SimPy:** Implement deterministic scheduled events, simulated time and event delivery. Pass delayed-shipment test B1.

**M3 — External businesses:** Implement supplier agents with private information and business constraints. Adapt CoffeeBench concepts and reusable code where appropriate. Pass supplier-disagreement test C1.

**M4 — E-Commerce:** Adapt selected negotiation, market and disruption components without creating a second authoritative accounting system. Pass supplier-repricing test D1.

**M5 — Customer interactions:** Integrate τ-bench-style customer simulation and policy validation. Pass complaint test E1.

**M6 — Integrated evaluation:** Execute scenario S01, a 14-simulated-day procurement investigation involving a missing shipment, conflicting supplier evidence, delivery delays, production consequences and invoice resolution.

## Required deliverables

- Architecture documentation and dependency map
- Version-pinned reproducible environment configuration
- Working service adapters and documented APIs
- Seed data and a resettable scenario database
- Actor identities, permissions and private information boundaries
- Simulated time and event scheduling
- Five passing integration tests
- One passing end-to-end simulation infrastructure test
- Evaluation reports with full trajectories, business-state checks, action costs and failure explanations
- Documentation describing how to replace the basic agent with a custom harness

## Acceptance conditions

The integration is complete when:

1. Each subsystem passes its independent test.
2. The combined 14-day scenario runs from start to finish.
3. Hidden scenario facts cannot be accessed directly by the tested agent.
4. ERPNext, messaging and simulation events remain consistent.
5. The environment can be reset and replayed.
6. The evaluator detects incomplete investigations and unauthorized actions.
7. The agent under test can be swapped without rebuilding the simulated environment.

Do not claim that the agent successfully performs the job until its performance is measured. A functioning integration and a successfully operating agent are separate acceptance criteria.

Start by auditing the upstream codebases and producing a compatibility report identifying reusable modules, required adapters, version conflicts and necessary custom implementations before modifying The Agent Company.

## 11. Reference repositories

These are the repositories I'd have your implementation agents inspect first.

- [The Agent Company](https://github.com/TheAgentCompany/TheAgentCompany?utm_source=chatgpt.com) — baseline infrastructure and task evaluation. Its setup documentation covers Rocket.Chat, ownCloud, GitLab and Plane.&#x20;

  [image](https://www.google.com/s2/favicons?domain=https://github.com\&sz=32)

  GitHub+1

- [ERPNext](https://github.com/frappe/erpnext?utm_source=chatgpt.com) and [Frappe REST API](https://docs.frappe.io/framework/user/en/guides/integration/rest_api?utm_source=chatgpt.com) — business transactions and API integration.

- [SimPy](https://simpy.readthedocs.io/?utm_source=chatgpt.com) — discrete-event scheduling.

- [CoffeeBench](https://github.com/SakanaAI/CoffeeBench?utm_source=chatgpt.com) — multi-company agent simulation.

- [E-Commerce Bench](https://github.com/QwenLM/E-CommerceBench?utm_source=chatgpt.com) — persistent store operations and deterministic economic modeling.

- [τ-bench](https://github.com/sierra-research/tau2-bench?utm_source=chatgpt.com) — simulated users, customer policies and custom domain support.

One final architecture decision I'd emphasize: don't merge the six benchmark codebases into one large application. Build a simulation coordinator around The Agent Company, expose the other environments through clearly defined adapters, and reuse only the components each does well.

That will let you add further domains—construction projects, employee relocation, financial close, order-to-cash—without having to rebuild the testing environment every time.