# Run results and evaluation history

Financial company scenarios expose a **Results** section on their run page. It records one immutable evaluation per request, identifies the native benchmark and version, labels native versus company-adapted evaluation, and preserves the benchmark's own scale. Per-criterion checks and recorded values are shown without calculating a combined percentage.

## Result states

- **Scored** (`completed`): evaluation finished. A native score of `0` is a valid score and is displayed as `0`; it is not treated as an evaluation failure.
- **Incomplete**: the evaluator could not produce a complete grade. Any available raw values remain visible, and missing values are not replaced with zero.
- **Could not evaluate** (`failed`): the evaluation operation failed and did not produce a score.
- **Queued**, **Running**, or **Outcome uncertain**: the durable evaluation request has no final outcome yet. Refresh checks that same report.
- **Not evaluated**: no evaluation has been recorded. Workplace runs show this state because they have no benchmark evaluator.

History links open the individual report for each evaluation ID. Starting a new evaluation creates a new history entry. If the request response is lost, **Retry evaluation request** reuses its original idempotency key; once the coordinator returns a durable evaluation ID, the next Evaluate action starts a new evaluation.

The dashboard receives only the public score DTO. It does not display evaluator prompts, raw evidence snapshots, or full evaluator artifacts. Evaluation requests and reads use the server-side coordinator client; the coordinator bearer token is not sent to the browser. Coordinator reads require controller or evaluator authorization.

## API flow

The dashboard associates each local simulation run with its coordinator run ID. The browser calls the Experiments Flask application, which proxies the operation with its private controller token:

- `POST /runs/{local_run_id}/evaluate` starts an evaluation. The form includes the Flask CSRF token and an `idempotency_key`.
- `GET /api/runs/{local_run_id}/evaluations` returns the report summaries in history order.
- `GET /api/runs/{local_run_id}/evaluations/latest` returns the latest report or an explicit empty result.
- `GET /api/runs/{local_run_id}/evaluations/{evaluation_id}` reads one report and verifies it belongs to that run.

The coordinator stores reservations, lifecycle events, safe scores, and criterion outcomes durably. Its authenticated service routes are `POST /scenarios/{run_id}/evaluate` (the legacy alias), `POST /runs/{run_id}/evaluations`, `GET /runs/{run_id}/evaluations`, `GET /runs/{run_id}/evaluations/latest`, and `GET /evaluations/{evaluation_id}`. The write requires an `Idempotency-Key`; retries with the same key return the same reserved evaluation, including after a run generation changes.

The coordinator's private input snapshot and full evaluator result are retained in its private evidence directory for audit and recovery. They are not part of the dashboard score DTO.
