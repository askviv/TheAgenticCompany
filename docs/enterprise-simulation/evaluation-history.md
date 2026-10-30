# Durable evaluation history

The coordinator's existing SQLite database owns evaluation reservations, lifecycle events and immutable outcomes. New tables and forward additions are installed at startup; append-only triggers prevent edits or deletion of earlier scores. This does not complete any benchmark's operational feedback coverage.

Controller/evaluator credentials can use:

- `POST /runs/{run_id}/evaluations` (or existing `/scenarios/{run_id}/evaluate`) with an `Idempotency-Key` header. An optional `generation` query binds a new request to the current generation. Retrying a previously reserved key returns that original evaluation, including after reset.
- `GET /runs/{run_id}/evaluations`: history across generations; optional `generation` filters it.
- `GET /runs/{run_id}/evaluations/latest`: current generation by default, `{ "evaluation": null }` when absent.
- `GET /evaluations/{evaluation_id}`: saved public report.

Agent/Viv principals cannot use these routes. The Experiments backend keeps the dashboard controller credential server-side and projects only the public score contract. See [Results UI](../experiments-results.md).

A request reserves its ID before capturing evidence. ERPNext's native read-only repeatable-read snapshot supplies the relevant linked records and version hash. Legacy evaluator read sets are bracketed with native version snapshots; inconsistent capture is incomplete, never a zero. The run's cases, audit, messages, clock and evaluator inputs are frozen. Slow model judging runs without holding the company lock, and subsequent company activity does not replace those captured inputs. The original evaluator criteria are retained.

Private input/result artifacts live under `state_root/evaluation-evidence`, with directory mode 0700 and file mode 0600. Public score records contain opaque evidence references and SHA-256 hashes, not message bodies, prompts, credentials or reasoning. Public outcome metadata is stored in SQLite independently of those private artifacts. Back up both the SQLite database and private evidence directory to retain reproducibility; restoring an old database must never overwrite newer company work.

The same caller/run/idempotency key cannot judge twice. A fresh evaluation requires a new key and snapshot. Startup reconciliation uses an evaluation-specific process lock: recover a fully written result if possible, otherwise retain an interrupted attempt as incomplete/uncertain without repeating model judging. Definite integration exceptions are failed and ungraded. A completed native reward of zero remains a real scored outcome; missing mappings, incomplete evidence and integration failures cannot become a pass or a fabricated zero.

Each score retains its native scale. There is no combined percentage across benchmarks. Company/email/ERP adapters are labelled company-adapted, with explicit mapping warnings. Unsupported CoffeeBench/E-CommerceBench scores are not invented merely because a result can now be stored.

Verification uses synthetic evidence and controlled evaluator responses: database reopen, concurrent idempotency, append-only protection, changed evidence, generation boundaries, permission denial, interrupted-result recovery, and public/private separation. The native read-only ERP snapshot proof is recorded in `deploy/enterprise-simulation/erpnext/admin/verification-results/evaluation_evidence_snapshot.json`. No experiment, live benchmark episode or Viv execution is needed for these checks.

## Deployment verification — 30 October 2026

Deployed on the existing mini without creating a company run or evaluation. Coordinator history/latest routes returned HTTP 200 with empty history/null latest; agent access to both returned 403. SQLite contained zero evaluation reservations/outcomes and zero company experiments. Dashboard health, run page, compiled JavaScript and history proxy returned 200; coordinator and actor services were active, and scheduled maintenance succeeded.

Validation: 277 coordinator/company/mail/ERP-adapter/dashboard integration tests and 115 subtests passed; another 30 tests passed in the isolated environment containing the pinned τ package. React TypeScript/Vite production builds passed locally and on the host. These use synthetic evidence and controlled evaluator responses; native ERP checks are read-only or rolled back. No live benchmark episode or Viv test was run. A browser-rendered report with a live score was deliberately not produced.

Pre-deployment SQLite/source backup: `/data/theagentcompany/enterprise/backups/evaluation-history-20261030T073634Z`. The previous native-feedback activation backup is retained separately. Stop only the affected application services before a code rollback; preserve the append-only score tables and private evidence rather than replacing the current database with this older backup.

## Non-finite native result handling

Non-finite float or Decimal metrics are malformed evaluator output. They produce
a durable failed, ungraded result rather than being silently omitted from a
completed score. Private artifacts retain the source diagnostic; public scores
and criterion fields cannot contain NaN or infinity. Finite Decimal metrics keep
their exact text, including trailing zeros.

The four synthetic invalid-value regressions failed against the previous score
projection (incorrectly completed), then passed with the fix. All 38 evaluation
service/history/snapshot/API/background checks passed. Reopening the database
and retrying the same key returns the failed record without judging again. No
live evaluation or benchmark episode was invoked.
