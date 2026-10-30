# Background provider controls

The Company activity page has separate **Start/Stop CoffeeBench** and
**Start/Stop E-CommerceBench** controls.
Each control affects that provider’s existing shared-company activity, not the
whole company or an experiment. Company email and the other enabled provider
remain available.

Stop is durable across coordinator restarts. It prevents new advances for that provider
and retains native checkpoints, pending receipts and history. An operation already
in flight may finish; the UI reports **Stopping** until that boundary completes.
No process is killed and no native data is reset.

Start queues work on the existing background worker and resumes the same source
state. CoffeeBench uses the audited recovery contract for an interrupted operation;
E-Commerce resumes its existing pending request or scheduled source update.
Recovery retains its operation/key across failed retries. A recovery failure is
visible and leaves CoffeeBench stopped; another Start can retry without inventing
a fresh company or silently repeating uncertain judging. Normal background work
can invoke the configured model and create company activity.

The controller-only endpoint is `POST /company/background/{provider_id}/control` (`coffeebench` or `ecommercebench`),
with JSON `{"enabled": true}` or `{"enabled": false}` and `Idempotency-Key`.
The dashboard BFF keeps credentials server-side and requires operator login and
CSRF protection. Replaying an old command cannot reverse a newer command.

Verification uses controlled workers, source-state fixtures and HTTP boundaries:
stop persistence, E-Commerce independence, paused-clock recovery, in-flight stop,
idempotent commands, recovery-key reuse and access denial. No live Start, model
turn, benchmark episode, experiment or Viv test is used for deployment validation.
CoffeeBench is left stopped after deployment.

## Deployment readback — 30 October 2026

Commit `b67ca5e` is deployed. The authenticated dashboard company endpoint reports
CoffeeBench `enabled=false`, `in_flight=false`, `status=stopped`; E-Commerce remains
active. Both coordinator and dashboard Stop routes succeeded. The pending native
operation hash matches the pre-change backup, and evaluation attempts remain zero.
The production bundle contains both controls; no live Start was sent. The
pre-change source/database backup is at
`/data/theagentcompany/enterprise/backups/coffee-controls-b67ca5e`.

Controlled validation: 21 background lifecycle checks, recovery/handoff checks,
and 46 BFF/client checks (126 subtests) passed. The production image build passed.
Native visual browser review was unavailable; deployed API and bundle readback
were verified instead.

E-Commerce controls reuse the same SQLite state, authenticated routes and UI
components. Provider selection is explicit before dispatch; the native runtime
cannot silently add E-Commerce work to a Coffee-only call. Coupled source work
is tracked as in flight before dispatch. Stopping E-Commerce does not create
replacement market facts: Coffee retains the existing source-freshness checks
and reports a source dependency if its next update needs newer E-Commerce data.

The addition preserves the current operating choices: CoffeeBench stopped and
E-Commerce active. Deployment checks do not toggle E-Commerce or invoke a live
Start, model, experiment or evaluator.

## E-Commerce deployment readback — 30 October 2026

Commit `13f19c4` is deployed. The authenticated dashboard reports CoffeeBench
stopped and E-Commerce active, both with enabled/in-flight control fields. The
native API advertises both allowed provider IDs, and the dashboard E-Commerce
route rejects invalid data without performing an action. Pending-operation and
provider-state hashes match the stopped-writer pre-change backup at
`/data/theagentcompany/enterprise/backups/ecommerce-controls-13f19c4`.

All 94 focused integration checks and 126 subtests passed, as did the production
image build. The explicit-provider regression also fails against the old runtime
for the expected unintended E-Commerce dispatch. The deployed bundle is
`index-Dfr-Wv3A.js`; both application health checks pass. No live Start/Stop
toggle, experiment, model turn, evaluation or Viv test was performed. Browser
visual review remains unavailable; readback covers the deployed APIs and bundle.
